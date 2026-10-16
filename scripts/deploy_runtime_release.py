"""Build an isolated, immutable ordinary WRF release. Default: read-only plan."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runtime_release import SCRIPTS, LEGACY_SCRIPTS

VALIDATION_CHECKS = (
    'release_files', 'fixed_dependencies', 'python_environment', 'build_contract',
    'binary_dependencies', 'runtime_tables', 'geography',
)

# This exact validator runs both before first activation and when resuming a
# retained transaction. No marker from an interrupted, partial check is trusted.
VALIDATION = 'REQUIRED_SCRIPTS = ' + repr(SCRIPTS) + '\nLEGACY_REQUIRED_SCRIPTS = ' + repr(LEGACY_SCRIPTS) + '\nVALIDATION_CHECKS = ' + repr(VALIDATION_CHECKS) + '\n' + r'''
import hashlib, importlib.util, json, os, re, shlex, stat, subprocess
from pathlib import Path
PROGRAMS = ('wrf/main/wrf.exe','wrf/main/real.exe','wps/geogrid.exe','wps/ungrib.exe','wps/metgrid.exe')
TABLES = ('wps/ungrib/Variable_Tables/Vtable.GFS','wps/ungrib/Variable_Tables/Vtable.ECMWF',
          'wps/geogrid/GEOGRID.TBL','wps/metgrid/METGRID.TBL','wrf/run/LANDUSE.TBL')
PYTHON_PROBE = """
import importlib.metadata, json
from pathlib import Path
import f90nml, netCDF4, eccodes
eccodes.codes_get_api_version()
dependencies = set()
for line in Path('/proc/self/maps').read_text().splitlines():
    fields = line.split(maxsplit=5)
    if len(fields)==6 and fields[5].startswith('/') and '.so' in Path(fields[5]).name:
        dependencies.add(str(Path(fields[5]).resolve()))
print(json.dumps({'packages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()},
                  'dependencies':sorted(dependencies)}))
"""
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''): h.update(block)
    return h.hexdigest()
def document_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def release_content(manifest):
    return {k:v for k,v in manifest.items() if k not in ('validation','fingerprint')}
def runtime_command(stage, command):
    proc=subprocess.run(['bash','-c','. '+shlex.quote(str(stage/'runtime.env'))+' && '+command],
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
    if proc.returncode:
        raise ValueError('Runtime verification command failed: '+proc.stderr[-1000:])
    return proc.stdout
def python_environment(stage, python):
    return json.loads(runtime_command(stage,shlex.quote(python)+' -B -c '+shlex.quote(PYTHON_PROBE)))
def binary_dependencies(stage, python, tools=None):
    programs=PROGRAMS+(python,)+tuple((tools or {}).values())
    dependencies={str(Path(name).resolve()) for name in (python,)+tuple((tools or {}).values())}
    for relative in programs:
        path=stage/relative
        if not path.is_file() or not os.access(str(path),os.X_OK):
            raise ValueError('Missing executable: '+relative)
        output=runtime_command(stage,'ldd '+shlex.quote(str(path)))
        if 'not found' in output:
            raise ValueError('Binary dependency gate failed: '+relative)
        # Include the ELF loader as well as libraries after an "=>" token.
        dependencies.update(str(Path(p).resolve()) for p in re.findall(r'(?<!\S)(/[^\s()]+)',output))
    return dependencies
def geography_contract(stage, geog, expected=None):
    helper=stage/'scripts/validate_geography.py'
    spec=importlib.util.spec_from_file_location('_wrf_release_geography',str(helper))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.validate_geography(stage/'wps/geogrid/GEOGRID.TBL',geog,expected)
def validate_release(stage, manifest, expected_release=None, require_complete=True):
    if manifest.get('schema')!='wrf-runtime-release-1': raise ValueError('Invalid release manifest schema')
    release_id=manifest.get('release_id','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',release_id): raise ValueError('Invalid release ID')
    if expected_release is not None and release_id!=expected_release: raise ValueError('Retained release ID differs from requested release')
    if not isinstance(manifest.get('minimum_free_bytes'),int) or manifest['minimum_free_bytes']<0:
        raise ValueError('Invalid release disk budget')
    if require_complete:
        marker=manifest.get('validation',{})
        if marker.get('schema')!='wrf-release-validation-1' or marker.get('complete') is not True or marker.get('checks')!=list(VALIDATION_CHECKS):
            raise ValueError('Retained transaction has no complete validation marker')
        if marker.get('content_sha256')!=document_digest(release_content(manifest)):
            raise ValueError('Retained validation content changed')
        if manifest.get('fingerprint')!=document_digest({k:v for k,v in manifest.items() if k!='fingerprint'}):
            raise ValueError('Retained manifest fingerprint changed')
    def hash_map(name):
        value=manifest.get(name)
        if not isinstance(value,dict) or not value or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for k,v in value.items()):
            raise ValueError('Invalid release '+name)
        return value
    files=hash_map('files')
    scripts=REQUIRED_SCRIPTS if 'geography' in manifest else LEGACY_REQUIRED_SCRIPTS
    required=set(PROGRAMS)|{'runtime.env'}|{'scripts/'+name for name in scripts}
    if not required.issubset(files): raise ValueError('Release file inventory is incomplete')
    for relative,digest in files.items():
        path=stage/relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink() or not path.is_file() or sha(path)!=digest:
            raise ValueError('Retained file changed or unsafe: '+relative)
    actual_files=set()
    for path in stage.rglob('*'):
        if path.is_symlink():
            target=path.resolve()
            if not target.exists() or stage.resolve() not in target.parents:
                raise ValueError('Release link escapes or is unresolved: '+str(path))
        elif path.is_file() and str(path.relative_to(stage))!='manifest.json': actual_files.add(str(path.relative_to(stage)))
    if actual_files!=set(files): raise ValueError('Release contains files outside the frozen inventory')
    if 'layout' in manifest: validate_thin_layout(stage,manifest)
    python=manifest.get('python','')
    if not isinstance(python,str) or not Path(python).is_absolute(): raise ValueError('Invalid fixed Python path')
    dependencies=hash_map('dependencies')
    for name,digest in dependencies.items():
        path=Path(name)
        if not path.is_absolute() or not path.is_file() or str(path.resolve())!=name or sha(path)!=digest:
            raise ValueError('Fixed dependency changed: '+name)
    packages=manifest.get('python_packages')
    if not isinstance(packages,dict) or not packages or any(not isinstance(k,str) or not isinstance(v,str) or not v for k,v in packages.items()):
        raise ValueError('Invalid Python package inventory')
    if not {'f90nml','netcdf4','eccodes'}.issubset({name.lower().replace('_','-') for name in packages}):
        raise ValueError('Required Python packages are missing')
    environment=python_environment(stage,python)
    if environment.get('packages')!=packages: raise ValueError('Fixed Python package inventory changed')
    tools=manifest.get('runtime_tools',{})
    if not isinstance(tools,dict) or set(tools)-{'grib_filter'} or any(not isinstance(p,str) or not Path(p).is_absolute() for p in tools.values()):
        raise ValueError('Invalid fixed runtime tools')
    actual_dependencies=binary_dependencies(stage,python,tools)|set(environment.get('dependencies',[]))
    if not actual_dependencies.issubset(dependencies): raise ValueError('Unpinned runtime shared dependency')
    build=manifest.get('build',{})
    if not isinstance(build,dict) or set(build)!= {'source_hash','binary_hashes','generator_hashes'}:
        raise ValueError('Invalid release build contract')
    if not isinstance(build['source_hash'],str) or not re.fullmatch('[0-9a-f]{64}',build['source_hash']):
        raise ValueError('Invalid build source hash')
    if set(build['binary_hashes'])!={'wrf','real','geogrid','ungrib','metgrid'} or set(build['generator_hashes'])!={'apply_namelist_plan.py','namelist_layout.py','wrf.sh'}:
        raise ValueError('Incomplete binary or generator build contract')
    spec=importlib.util.spec_from_file_location('_wrf_release_source_probe',str(stage/'scripts/namelist_source_probe.py'))
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    current=probe.snapshot(str(stage/'wrf'),str(stage/'wps'),str(stage/'scripts'))
    if any(current.get(k)!=v for k,v in build.items()): raise ValueError('Retained release build contract changed')
    for relative in TABLES:
        if not (stage/relative).is_file(): raise ValueError('Missing runtime table: '+relative)
    geog=Path(manifest.get('geog_path',''))
    if not geog.is_absolute() or not geog.is_dir():
        raise ValueError('Missing geography tables')
    if 'geography' in manifest:
        contract=manifest['geography']
        if not isinstance(contract,dict) or contract.get('schema')!='wrf-geography-1':
            raise ValueError('Invalid frozen geography contract')
        geography_contract(stage,geog,contract)
    elif not any(geog.glob('*/index')):
        raise ValueError('Missing geography tables')
def complete_validation(stage, manifest):
    validate_release(stage,manifest,require_complete=False)
    manifest['validation']={'schema':'wrf-release-validation-1','complete':True,
                            'content_sha256':document_digest(release_content(manifest)),
                            'checks':list(VALIDATION_CHECKS)}
    manifest['fingerprint']=document_digest({k:v for k,v in manifest.items() if k!='fingerprint'})
    (stage/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2))
def make_readonly(stage):
    # A failed initial seal can leave writable descendants. Always reseal all.
    for path in stage.rglob('*'):
        if path.is_symlink(): continue
        if path.is_file(): path.chmod(0o555 if os.access(str(path),os.X_OK) else 0o444)
        elif path.is_dir(): path.chmod(0o555)
def make_staging_writable(stage):
    # copytree preserves immutable source modes. Change only owned copies;
    # never follow source/runtime links while granting temporary write access.
    for base,dirs,files in os.walk(str(stage),followlinks=False):
        for path in [Path(base)]+[Path(base)/name for name in dirs+files]:
            original=os.lstat(str(path));mode=original.st_mode
            if stat.S_ISLNK(mode): continue
            if stat.S_ISDIR(mode): wanted=stat.S_IMODE(mode)|stat.S_IRUSR|stat.S_IWUSR|stat.S_IXUSR
            elif stat.S_ISREG(mode): wanted=stat.S_IMODE(mode)|stat.S_IRUSR|stat.S_IWUSR
            else: raise ValueError('Unsupported staging file type: '+str(path))
            flags=os.O_RDONLY|os.O_NOFOLLOW
            if stat.S_ISDIR(mode): flags|=os.O_DIRECTORY
            descriptor=os.open(str(path),flags)
            try:
                opened=os.fstat(descriptor)
                if (opened.st_dev,opened.st_ino,stat.S_IFMT(opened.st_mode))!=(original.st_dev,original.st_ino,stat.S_IFMT(mode)):
                    raise ValueError('Staging file changed while opening: '+str(path))
                os.fchmod(descriptor,wanted)
            finally: os.close(descriptor)
'''

THIN_SUPPORT = r'''
import fnmatch, shutil
NATIVE_METADATA = (
    'wrf/inc/namelist_statements.inc','wrf/inc/version_decl',
    'wps/geogrid/src/gridinfo_module.F','wps/metgrid/src/gridinfo_module.F',
    'wps/ungrib/src/read_namelist.F','wps/namelist.wps.all_options','wps/README',
)
RUN_EXCLUDES = (
    'real.exe','wrf.exe','wrfout_d*','wrfrst_d*','wrfinput_d*','wrfbdy_d*',
    'wrffdda_d*','met_em.d*','rsl.out.*','rsl.error.*','*.log','namelist.input',
)
def source_file(path, root):
    target=path.resolve(strict=True)
    if root not in target.parents or not target.is_file():
        raise ValueError('Unsafe thin source file: '+str(path))
    if not target.stat().st_size: raise ValueError('Empty thin source file: '+str(path))
    return target
def thin_native_inventory(wrf, wps):
    roots={'wrf':Path(wrf).resolve(strict=True),'wps':Path(wps).resolve(strict=True)}
    selected={}
    def add(relative):
        folder, name=relative.split('/',1)
        selected[relative]=sha(source_file(roots[folder]/name,roots[folder]))
    def tree(folder, relative, excludes=()):
        base=roots[folder]/relative
        if base.is_symlink() or not base.is_dir():
            raise ValueError('Missing physical thin source directory: '+str(base))
        for directory, dirs, files in os.walk(str(base),followlinks=False):
            for name in dirs+files:
                path=Path(directory)/name
                if any(fnmatch.fnmatchcase(name,pattern) for pattern in excludes):
                    if name in dirs: dirs.remove(name)
                    continue
                if name in dirs:
                    if path.is_symlink(): raise ValueError('Thin source directory link is unsupported: '+str(path))
                else: add(folder+'/'+str(path.relative_to(roots[folder])))
    for relative in PROGRAMS+('wps/link_grib.csh',)+NATIVE_METADATA: add(relative)
    tree('wrf','run',RUN_EXCLUDES)
    tree('wps','ungrib/Variable_Tables')
    for folder,pattern in [('geogrid','GEOGRID.TBL*'),('metgrid','METGRID.TBL*')]:
        for path in sorted((roots['wps']/folder).glob(pattern)):
            add('wps/'+str(path.relative_to(roots['wps'])))
    registry=roots['wrf']/'Registry'
    if registry.is_symlink() or not registry.is_dir(): raise ValueError('Missing physical Registry metadata')
    for path in sorted(registry.glob('*')):
        if path.suffix=='.var' or not path.is_file(): continue
        target=source_file(path,roots['wrf'])
        if any(line.split(maxsplit=1)[:1]==['rconfig'] for line in target.read_text(errors='replace').splitlines()):
            add('wrf/Registry/'+path.name)
    if not any(name.startswith('wrf/Registry/') for name in selected): raise ValueError('Registry rconfig metadata is missing')
    for relative in TABLES:
        if relative not in selected: raise ValueError('Missing thin runtime table: '+relative)
    return dict(sorted(selected.items()))
def read_source_anchor(root):
    root=root.resolve(strict=True)
    source=root/'source';anchor_path=source/'anchor.json'
    if source.is_symlink() or not source.is_dir() or anchor_path.is_symlink() or not anchor_path.is_file():
        raise ValueError('Thin release requires a fixed source anchor')
    if source.stat().st_mode & 0o222 or anchor_path.stat().st_mode & 0o222:
        raise ValueError('Source anchor is not sealed read-only')
    anchor=json.loads(anchor_path.read_text())
    if anchor.get('schema')!='wrf-source-anchor-1' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',anchor.get('release_id','')):
        raise ValueError('Invalid source anchor manifest')
    release=root/'releases'/anchor['release_id'];manifest_path=release/'manifest.json'
    if release.is_symlink() or not release.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Source anchor release is not physical')
    if release.stat().st_mode & 0o222 or manifest_path.stat().st_mode & 0o222:
        raise ValueError('Source anchor release is not sealed read-only')
    manifest=json.loads(manifest_path.read_text())
    marker=manifest.get('validation',{})
    if manifest.get('layout',{}).get('mode')=='thin': raise ValueError('A thin release cannot supply authoritative source')
    if (manifest.get('schema')!='wrf-runtime-release-1' or manifest.get('release_id')!=anchor['release_id']
            or manifest.get('fingerprint')!=document_digest({k:v for k,v in manifest.items() if k!='fingerprint'})
            or marker.get('schema')!='wrf-release-validation-1' or marker.get('complete') is not True
            or marker.get('checks')!=list(VALIDATION_CHECKS)
            or marker.get('content_sha256')!=document_digest(release_content(manifest))
            or sha(manifest_path)!=anchor.get('release_manifest_sha256')
            or manifest['fingerprint']!=anchor.get('release_fingerprint')
            or anchor.get('release_path')!=str(release) or anchor.get('protected_release_paths')!=[str(release)]
            or anchor.get('source_trees_readonly') is not True
            or anchor.get('configuration_source_hash')!=manifest.get('build',{}).get('source_hash')):
        raise ValueError('Source anchor release binding changed')
    expected=[]
    for folder,name,version in [('wrf','wrf-4.7.1','4.7.1'),('wps','wps-4.6.0','4.6.0')]:
        target=release/folder;link=source/name
        if target.is_symlink() or not target.is_dir() or not link.is_symlink() or os.readlink(str(link))!=str(target):
            raise ValueError('Source anchor link changed: '+name)
        inventory={key[len(folder)+1:]:value for key,value in manifest['files'].items() if key.startswith(folder+'/')}
        expected.append({'name':name,'version':version,'path':str(link),'target':str(target),
                         'file_count':len(inventory),'source_inventory_sha256':document_digest(inventory)})
    if anchor.get('components')!=expected: raise ValueError('Source anchor component inventory changed')
    native=thin_native_inventory(release/'wrf',release/'wps')
    for name,digest in native.items():
        path=release/name
        if (path.is_symlink() or path.stat().st_mode & 0o222 or manifest['files'].get(name)!=digest
                or any(parent.stat().st_mode & 0o222 for parent in path.parents if release in (parent,*parent.parents))):
            raise ValueError('Anchored native source file changed: '+name)
    binding={'path':str(anchor_path),'sha256':sha(anchor_path),'digest':document_digest(anchor),
             'release_id':anchor['release_id'],'release_fingerprint':manifest['fingerprint'],
             'release_manifest_sha256':sha(manifest_path)}
    return binding,native,manifest['build']
def copy_thin_native(stage, wrf, wps, inventory):
    roots={'wrf':Path(wrf).resolve(strict=True),'wps':Path(wps).resolve(strict=True)}
    for relative,digest in inventory.items():
        folder,name=relative.split('/',1)
        original=source_file(roots[folder]/name,roots[folder]);target=stage/relative
        if sha(original)!=digest: raise ValueError('Thin source changed before copy: '+relative)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(str(original),str(target))
        if target.is_symlink() or sha(target)!=digest or sha(original)!=digest:
            raise ValueError('Thin source changed during copy: '+relative)
def thin_interpreter_dependencies(stage):
    linker=stage/'wps/link_grib.csh'
    if not os.access(str(linker),os.X_OK): raise ValueError('GRIB linker is not executable')
    first=linker.read_text(errors='replace').splitlines()[0]
    command=shlex.split(first[2:]) if first.startswith('#!') else []
    if not command or not Path(command[0]).is_absolute() or Path(command[0]).name=='env':
        raise ValueError('GRIB linker requires a fixed absolute interpreter')
    interpreter=Path(command[0]).resolve(strict=True)
    if not interpreter.is_file() or not os.access(str(interpreter),os.X_OK):
        raise ValueError('GRIB linker interpreter is not executable')
    output=runtime_command(stage,'ldd '+shlex.quote(str(interpreter)))
    if 'not found' in output: raise ValueError('GRIB linker interpreter dependency gate failed')
    return {str(interpreter)}|{str(Path(p).resolve()) for p in re.findall(r'(?<!\S)(/[^\s()]+)',output)}
def validate_thin_layout(stage,manifest):
    layout=manifest['layout']
    if (not isinstance(layout,dict) or set(layout)!={'schema','mode','native_files','native_inventory_sha256','source_anchor'}
            or layout.get('schema')!='wrf-runtime-layout-1' or layout.get('mode')!='thin'):
        raise ValueError('Invalid thin runtime layout')
    binding,anchored,build=read_source_anchor(stage.parent.parent)
    native=layout.get('native_files')
    if native!=anchored or layout.get('native_inventory_sha256')!=document_digest(anchored) or layout.get('source_anchor')!=binding:
        raise ValueError('Thin native inventory differs from fixed source anchor')
    if any(manifest.get('build',{}).get(key)!=build[key] for key in ('source_hash','binary_hashes')):
        raise ValueError('Thin release build differs from fixed source anchor')
    for path in stage.rglob('*'):
        if path.is_symlink(): raise ValueError('Thin runtime links are prohibited: '+str(path))
        if not path.is_file() and not path.is_dir(): raise ValueError('Unsupported thin runtime file type: '+str(path))
    if not thin_interpreter_dependencies(stage).issubset(manifest.get('dependencies',{})):
        raise ValueError('Unpinned GRIB linker interpreter dependency')
    actual={}
    for folder in ('wrf','wps'):
        base=stage/folder
        if base.is_symlink() or not base.is_dir(): raise ValueError('Thin runtime tree must be physical: '+folder)
        for path in base.rglob('*'):
            if path.is_symlink(): raise ValueError('Thin runtime links are prohibited: '+str(path))
            if path.is_file(): actual[str(path.relative_to(stage))]=sha(path)
            elif not path.is_dir(): raise ValueError('Unsupported thin runtime file type: '+str(path))
    if actual!=native: raise ValueError('Thin runtime inventory is incomplete or contains extra files')
    if thin_native_inventory(stage/'wrf',stage/'wps')!=native:
        raise ValueError('Thin runtime selection changed')
'''
VALIDATION += THIN_SUPPORT

REMOTE = VALIDATION + r'''
import fcntl, hashlib, json, os, shutil, subprocess, sys, re
from pathlib import Path
root, data, stage = map(Path, sys.argv[1:4])
cfg = json.loads(sys.argv[4])
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''): h.update(block)
    return h.hexdigest()
with (root/'deployment.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    if stage.parent != root/'staging' or stage.is_symlink(): raise ValueError('Invalid staging root')
    if any((stage/name).exists() for name in ('wrf','wps')): raise ValueError('Staging already populated; inspect retained transaction')
    exclude=shutil.ignore_patterns('wrfout*','wrfrst*','wrfinput*','wrfbdy*','met_em*','rsl.*','GFS:*','PFILE:*','FILE:*','GRIBFILE.*','*.log','*.o','*.mod','*.a','.git','__pycache__')
    def source_exclude(directory, names):
        ignored=exclude(directory,names)
        # WRFDA coefficient data are outside the ordinary ARW service. Preserve
        # its source files, without importing a separate runtime through a link.
        if str(Path(directory)).startswith(str(Path(cfg['wrf'])/'var')+'/'):
            ignored.update(n for n in names if (Path(directory)/n).is_symlink())
        return ignored
    source=Path(cfg['wps'])
    layout=None
    if cfg.get('thin',False):
        binding,native,anchored_build=read_source_anchor(root)
        if thin_native_inventory(cfg['wrf'],cfg['wps'])!=native:
            raise ValueError('Requested thin source differs from fixed source anchor')
        copy_thin_native(stage,cfg['wrf'],cfg['wps'],native)
        layout={'schema':'wrf-runtime-layout-1','mode':'thin','native_files':native,
                'native_inventory_sha256':document_digest(native),'source_anchor':binding}
    else:
        shutil.copytree(cfg['wrf'], str(stage/'wrf'), symlinks=True, ignore=source_exclude)
        (stage/'wps').mkdir()
        for child in source.iterdir():
            if child.name not in ('geogrid','ungrib','metgrid','util','external','arch','configure','compile','clean','configure.wps','link_grib.csh','README','README.md','namelist.wps.all_options'):
                continue
            if child.is_dir(): shutil.copytree(str(child),str(stage/'wps'/child.name),symlinks=True,ignore=exclude)
            elif child.is_file(): shutil.copy2(str(child),str(stage/'wps'/child.name))
    make_staging_writable(stage)
    for relative, original in [('wrf/main/wrf.exe',Path(cfg['wrf'])/'main/wrf.exe'),('wrf/main/real.exe',Path(cfg['wrf'])/'main/real.exe'),
                               ('wps/geogrid.exe',source/'geogrid.exe'),('wps/ungrib.exe',source/'ungrib.exe'),('wps/metgrid.exe',source/'metgrid.exe')]:
        target=stage/relative
        if target.is_symlink(): target.unlink()
        shutil.copy2(str(original.resolve()),str(target))
        if sha(target)!=sha(original): raise ValueError('Copied binary fingerprint mismatch')
    # Internal links are relocated; external runtime-table links are materialized.
    for base, dirs, files in os.walk(str(stage),followlinks=False):
        for name in dirs+files:
            path=Path(base)/name
            if not path.is_symlink(): continue
            target=path.resolve()
            source_roots=[Path(cfg['wrf']),Path(cfg['wps'])]
            relocated=None
            for src,dst in zip(source_roots,(stage/'wrf',stage/'wps')):
                try: relocated=dst/target.relative_to(src.resolve()); break
                except ValueError: pass
            path.unlink()
            if relocated is not None and relocated.exists():
                path.symlink_to(os.path.relpath(str(relocated),str(path.parent)),target_is_directory=relocated.is_dir())
            elif target.is_file(): shutil.copy2(str(target),str(path))
            else: raise ValueError('Unresolved or external directory link: '+str(path))
    for folder in ('forcing/gfs','forcing/ecmwf','tasks','products'):
        (data/folder).mkdir(parents=True,exist_ok=True)
    for rel,digest in cfg['scripts'].items():
        if sha(stage/'scripts'/rel)!=digest: raise ValueError('Uploaded script fingerprint mismatch')
    env=dict(cfg['environment'])
    env['PATH']=str(Path(cfg['python']).parent)+':'+env.get('PATH','/usr/bin:/bin')
    tools={}
    if env.get('ECCODES_DIR'):
        env['WRF_GRIB_FILTER']=str(Path(env['ECCODES_DIR'])/'bin/grib_filter')
        tools['grib_filter']=env['WRF_GRIB_FILTER']
    import shlex
    (stage/'runtime.env').write_text('\n'.join('export '+k+'='+shlex.quote(v) for k,v in sorted(env.items()))+'\n')
    manifest={'schema':'wrf-runtime-release-1','release_id':cfg['release_id'],'geog_path':cfg['geog'],
              'minimum_free_bytes':cfg['minimum_free_bytes'],'python':cfg['python'],'runtime_tools':tools,'files':{}}
    manifest['geography']=geography_contract(stage,Path(cfg['geog']))
    if layout is not None: manifest['layout']=layout
    sys.path.insert(0,str(stage/'scripts'))
    from namelist_source_probe import snapshot
    build=snapshot(str(stage/'wrf'),str(stage/'wps'),str(stage/'scripts'))
    manifest['build']={k:build[k] for k in ('source_hash','binary_hashes','generator_hashes')}
    environment=python_environment(stage,cfg['python'])
    manifest['python_packages']=environment['packages']
    dependencies=binary_dependencies(stage,cfg['python'],tools)|set(environment['dependencies'])
    if layout is not None: dependencies|=thin_interpreter_dependencies(stage)
    if env.get('ECCODES_DIR'): dependencies.add(str((Path(env['ECCODES_DIR'])/'lib/libeccodes.so').resolve()))
    manifest['dependencies']={}
    for path in sorted(stage.rglob('*')):
        if path.is_file() and not path.is_symlink(): manifest['files'][str(path.relative_to(stage))]=sha(path)
    for dependency in sorted(dependencies): manifest['dependencies'][dependency]=sha(Path(dependency))
    complete_validation(stage,manifest)
    target=root/'releases'/cfg['release_id']
    if target.exists(): raise ValueError('Immutable release already exists')
    make_readonly(stage)
    os.rename(str(stage),str(target))
    target.chmod(0o555)
    current=root/('current.'+cfg['release_id']); current.symlink_to(target)
    os.replace(str(current),str(root/'current'))
    (root/'manifests'/('release-'+cfg['release_id']+'.json')).write_text(json.dumps(manifest,sort_keys=True,indent=2))
    print(json.dumps({'status':'installed','release_id':cfg['release_id'],'fingerprint':manifest['fingerprint'],'files':len(manifest['files'])}))
'''

RESUME = VALIDATION + r'''
import fcntl,hashlib,json,os,sys
from pathlib import Path
root,stage=Path(sys.argv[1]),Path(sys.argv[2])
expected_release=sys.argv[3]
with (root/'deployment.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if stage.parent!=root/'staging' or stage.is_symlink(): raise ValueError('Invalid transaction')
    manifest=json.loads((stage/'manifest.json').read_text())
    validate_release(stage,manifest,expected_release)
    target=root/'releases'/manifest['release_id']
    if target.exists(): raise ValueError('Release already exists; inspect instead of replay')
    make_readonly(stage)
    stage.chmod(0o755);os.rename(str(stage),str(target));target.chmod(0o555)
    temporary=root/('current.'+manifest['release_id']);temporary.symlink_to(target)
    os.replace(str(temporary),str(root/'current'))
    (root/'manifests'/('release-'+manifest['release_id']+'.json')).write_text(json.dumps(manifest,sort_keys=True,indent=2))
    print(json.dumps({'status':'installed','release_id':manifest['release_id'],'fingerprint':manifest['fingerprint'],'files':len(manifest['files'])}))
'''


def deploy(client, settings, root, data, python, release_id, apply=False, eccodes_root=None, thin=False):
    from hpc_transport import SAFE_REMOTE_ROOT
    import re
    if not all(SAFE_REMOTE_ROOT.fullmatch(p) and p.startswith('/') for p in (root,data,python)):
        raise ValueError('Deployment paths must be absolute and shell-safe')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',release_id): raise ValueError('Invalid release ID')
    directory=Path(__file__).parent
    scripts={name:hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in SCRIPTS}
    config={'release_id':release_id,'wrf':settings.hpc_wrf_source_dir,'wps':settings.hpc_wps_source_dir,
            'geog':settings.hpc_geog_dir,'python':python,'minimum_free_bytes':int(settings.hpc_runtime_min_free_gb*1024**3),
            'scripts':scripts}
    if thin: config['thin']=True
    if not apply: return {'mode':'plan','system_root':root,'data_root':data,'config':config}
    # Deployment copies binaries; it neither stops nor launches computations.
    command='. '+shlex.quote(settings.hpc_runtime_env)+" >/dev/null 2>&1 && python3 -c "+shlex.quote(
        "import json,os; names=('PATH','LD_LIBRARY_PATH','NETCDF','HDF5','JASPERLIB','JASPERINC','NCARG_ROOT','OMP_NUM_THREADS'); print(json.dumps({k:os.environ[k] for k in names if k in os.environ}))")
    config['environment']=json.loads(client.run(command,timeout=30))
    if eccodes_root:
        if not SAFE_REMOTE_ROOT.fullmatch(eccodes_root): raise ValueError('Invalid fixed ecCodes root')
        config['environment']['ECCODES_DIR']=eccodes_root
    stage=root+'/staging/'+uuid.uuid4().hex
    client.run('mkdir -p -- '+' '.join(shlex.quote(p) for p in (root+'/staging',root+'/releases',root+'/manifests',stage+'/scripts',data)),timeout=30)
    for name in SCRIPTS: client.upload(directory/name,stage+'/scripts/'+name,timeout=60)
    return json.loads(client.run(shlex.quote(python)+' -B -c '+shlex.quote(REMOTE)+' '+' '.join(shlex.quote(v) for v in (root,data,stage,json.dumps(config))),timeout=600))


def resume(client, root, transaction, python, release_id):
    from hpc_transport import SAFE_REMOTE_ROOT
    import re
    if not all(SAFE_REMOTE_ROOT.fullmatch(p) and p.startswith('/') for p in (root,python)):
        raise ValueError('Recovery paths must be absolute and shell-safe')
    if not re.fullmatch('[0-9a-f]{32}',transaction): raise ValueError('Invalid transaction token')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',release_id): raise ValueError('Invalid release ID')
    command=shlex.quote(python)+' -B -c '+shlex.quote(RESUME)+' '
    command+=' '.join(shlex.quote(value) for value in (root,root+'/staging/'+transaction,release_id))
    return json.loads(client.run(command,timeout=600))


ANCHOR_SOURCE = VALIDATION + r'''
import fcntl, sys
root, release_id, apply = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'apply'
release = root/'releases'/release_id
source = root/'source'
if root.is_symlink() or release.is_symlink() or not release.is_dir():
    raise ValueError('Source anchor requires an existing physical release')
with (root/'deployment.lock').open('a' if apply else 'r') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX if apply else fcntl.LOCK_SH)
    manifest_path = release/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('layout',{}).get('mode')=='thin':
        raise ValueError('A thin release cannot supply authoritative source')
    validate_release(release,manifest,expected_release=release_id)
    if any(path.stat().st_mode & 0o222 for path in [release]+list(release.rglob('*')) if not path.is_symlink()):
        raise ValueError('Source anchor release must be sealed read-only')
    versions = [('wrf','wrf-4.7.1','4.7.1','inc/version_decl'),
                ('wps','wps-4.6.0','4.6.0','README')]
    components = []
    for folder, name, version, declaration in versions:
        target = release/folder
        if target.is_symlink() or not target.is_dir():
            raise ValueError('Source component must be a physical release tree')
        text = (target/declaration).read_text()
        if not re.search(r'(?<![0-9.])V?'+re.escape(version)+r'(?![0-9.])',text,re.IGNORECASE):
            raise ValueError('Source version differs: '+name)
        inventory = {key[len(folder)+1:]:value for key,value in manifest['files'].items() if key.startswith(folder+'/')}
        if not inventory: raise ValueError('Source tree is absent from the frozen file inventory')
        components.append({'name':name,'version':version,'path':str(source/name),'target':str(target),
                           'file_count':len(inventory),'source_inventory_sha256':document_digest(inventory)})
    anchor = {'schema':'wrf-source-anchor-1','release_id':release_id,'release_path':str(release),
              'release_fingerprint':manifest['fingerprint'],'release_manifest_sha256':sha(manifest_path),
              'configuration_source_hash':manifest['build']['source_hash'],'components':components,
              'protected_release_paths':[str(release)],'source_trees_readonly':True,
              'sharing':'fixed links to sealed release trees; no source copy or current-link dependency'}
    exists = os.path.lexists(str(source))
    if exists:
        if source.is_symlink() or not source.is_dir() or set(os.listdir(str(source))) != {'anchor.json',*(item['name'] for item in components)}:
            raise ValueError('Existing source entry differs; no overwrite is permitted')
        for item in components:
            path = Path(item['path'])
            if not path.is_symlink() or os.readlink(str(path)) != item['target']:
                raise ValueError('Existing source link differs; no overwrite is permitted')
        anchor_path = source/'anchor.json'
        if anchor_path.is_symlink() or json.loads(anchor_path.read_text()) != anchor:
            raise ValueError('Existing source anchor manifest differs; no overwrite is permitted')
        if source.stat().st_mode & 0o222 or anchor_path.stat().st_mode & 0o222:
            raise ValueError('Existing source entry is not sealed read-only')
    elif apply:
        # mkdir is exclusive: a concurrent existing path is never replaced.
        source.mkdir(mode=0o700)
        for item in components:
            Path(item['path']).symlink_to(item['target'],target_is_directory=True)
        descriptor = os.open(str(source/'anchor.json'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444)
        with os.fdopen(descriptor,'w') as stream:
            json.dump(anchor,stream,sort_keys=True,indent=2)
            stream.flush();os.fsync(stream.fileno())
        (source/'anchor.json').chmod(0o444)
        source.chmod(0o555)
        directory = os.open(str(root),os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    print(json.dumps({'mode':'apply' if apply else 'plan','status':'already_anchored' if exists else 'anchored' if apply else 'ready_to_anchor',
                      'source_root':str(source),'anchor_manifest':anchor,'anchor_digest':document_digest(anchor),
                      'physical_source_bytes_copied':0,'current_link_changed':False}))
'''


def anchor_source(client, root, python, release_id, apply=False):
    from hpc_transport import SAFE_REMOTE_ROOT
    import re
    if not all(SAFE_REMOTE_ROOT.fullmatch(p) and p.startswith('/') for p in (root,python)):
        raise ValueError('Source anchor paths must be absolute and shell-safe')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',release_id):
        raise ValueError('Invalid source anchor release ID')
    command = shlex.quote(python)+' -B -c '+shlex.quote(ANCHOR_SOURCE)+' '
    command += ' '.join(shlex.quote(value) for value in (root,release_id,'apply' if apply else 'plan'))
    return json.loads(client.run(command,timeout=600))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('system-root','data-root','python','release-id'): parser.add_argument('--'+name,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--thin',action='store_true',help='Copy only the complete native runtime and source metadata pinned by the fixed source anchor; default remains a full source copy')
    parser.add_argument('--eccodes-root', help='Read-only prefix of the pinned ecCodes C library')
    parser.add_argument('--resume-staging', help='Verify and finish an already validated retained transaction')
    parser.add_argument('--anchor-source',action='store_true',help='Verify fixed version source links; add --apply to create the sealed source anchor without copying source trees')
    args=parser.parse_args()
    from config import settings
    from hpc_transport import HpcClient
    if args.anchor_source:
        if args.resume_staging or args.eccodes_root or args.thin:
            parser.error('--anchor-source cannot be combined with --resume-staging/--eccodes-root/--thin')
        print(json.dumps(anchor_source(HpcClient(settings),args.system_root,args.python,args.release_id,args.apply),ensure_ascii=False))
    elif args.resume_staging:
        import re
        if args.thin: parser.error('Resume uses the retained manifest layout; omit --thin')
        if not args.apply or not re.fullmatch('[0-9a-f]{32}',args.resume_staging): parser.error('Resume requires --apply and an exact transaction token')
        print(json.dumps(resume(HpcClient(settings),args.system_root,args.resume_staging,args.python,args.release_id),ensure_ascii=False))
    else:
        print(json.dumps(deploy(HpcClient(settings),settings,args.system_root,args.data_root,args.python,args.release_id,args.apply,args.eccodes_root,args.thin),ensure_ascii=False))
