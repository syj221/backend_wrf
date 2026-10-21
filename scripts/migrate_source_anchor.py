"""Operator-only independent source migration. Never alters tasks or workers."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys

sys.path.insert(0,str(Path(__file__).parent))
import deploy_runtime_release as deployment


OPERATIONS = r'''
import ctypes,fcntl,shutil,time,platform

def write_new_json(path, value, mode=0o600):
    fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,sort_keys=True,indent=2);stream.flush();os.fsync(stream.fileno())

def source_migration_plan(root):
    binding,native,build=read_source_anchor(root)
    if 'release_id' not in binding:raise ValueError('Source is already independent; inspect only')
    original=root/'releases'/binding['release_id']
    manifest=json.loads((original/'manifest.json').read_text())
    if any(p.is_symlink() or p.stat().st_mode & 0o222 or p.stat().st_uid!=os.getuid()
           for folder in ('wrf','wps') for p in [original/folder]+list((original/folder).rglob('*'))):
        raise ValueError('Original source is not physical, owned and sealed')
    for name,digest in manifest['files'].items():
        path=original/name
        if path.is_symlink() or not path.is_file() or sha(path)!=digest:raise ValueError('Original release content changed')
    bundle_id='wrf-4.7.1-wps-4.6.0-'+manifest['fingerprint'][:16]
    expected={name:d for name,d in manifest['files'].items() if name.startswith(('wrf/','wps/'))}
    size=sum((original/name).stat().st_size for name in expected)
    free=os.statvfs(str(root));free=free.f_bavail*free.f_frsize
    if free<size+1024**3:raise ValueError('Insufficient independent source copy space')
    value={'schema':'wrf-source-migration-1','root':str(root),'original_release':str(original),
           'original_identity':[original.stat().st_dev,original.stat().st_ino],
           'binding':binding,'build':build,'native':native,'source_files':len(expected),'source_bytes':size,
           'bundle_id':bundle_id,'bundle':str(root/'source-store'/bundle_id)}
    value['digest']=document_digest(value)
    return value

def prepare_source_migration(root,plan):
    if source_migration_plan(root)!=plan:raise ValueError('Source migration plan changed')
    bundle=Path(plan['bundle']);original=Path(plan['original_release'])
    if bundle.exists() or bundle.is_symlink():raise ValueError('Bundle already exists; inspect, never replay')
    store=bundle.parent
    if store.is_symlink() or store.resolve()!=store:raise ValueError('Unsafe source store')
    store.mkdir(mode=0o755,exist_ok=True)
    if store.stat().st_uid!=os.getuid():raise ValueError('Source store ownership mismatch')
    bundle.mkdir(mode=0o700)
    for folder in ('wrf','wps'):shutil.copytree(str(original/folder),str(bundle/folder),symlinks=False)
    provenance=bundle/'provenance';provenance.mkdir()
    shutil.copy2(str(original/'manifest.json'),str(provenance/'release-manifest.json'))
    shutil.copy2(str(root/'source/anchor.json'),str(provenance/'source-anchor-v1.json'))
    old=json.loads((provenance/'release-manifest.json').read_text())
    files={str(p.relative_to(bundle)):sha(p) for p in bundle.rglob('*') if p.is_file()}
    expected={k:v for k,v in old['files'].items() if k.startswith(('wrf/','wps/'))}
    if {k:v for k,v in files.items() if k.startswith(('wrf/','wps/'))}!=expected:raise ValueError('Copied source differs')
    for name in expected:
        if sha(original/name)!=expected[name] or (original/name).stat().st_ino==(bundle/name).stat().st_ino:
            raise ValueError('Source changed during copy or was hardlinked')
    manifest={'schema':'wrf-source-bundle-1','bundle_id':plan['bundle_id'],'build':plan['build'],
              'files':files,'legacy_binding':plan['binding']}
    manifest['fingerprint']=document_digest(manifest)
    write_new_json(bundle/'manifest.json',manifest)
    make_readonly(bundle);bundle.chmod(0o555)
    old_anchor=json.loads((provenance/'source-anchor-v1.json').read_text())
    anchor={'schema':'wrf-source-anchor-2','bundle_id':plan['bundle_id'],'bundle_path':str(bundle),
            'bundle_manifest_sha256':sha(bundle/'manifest.json'),
            'configuration_source_hash':plan['build']['source_hash'],'protected_release_paths':[],
            'source_trees_readonly':True,'components':[]}
    for item in old_anchor['components']:
        folder='wrf' if item['name']=='wrf-4.7.1' else 'wps'
        anchor['components'].append(dict(item,target=str(bundle/folder)))
    candidate=root/('.source-next-'+plan['digest'][:16])
    if candidate.exists() or candidate.is_symlink():raise ValueError('Source switch candidate already exists')
    candidate.mkdir(mode=0o700)
    for item in anchor['components']:(candidate/item['name']).symlink_to(item['target'],target_is_directory=True)
    write_new_json(candidate/'anchor.json',anchor,0o444);candidate.chmod(0o555)
    # Validate against the stable logical paths, without activating the candidate.
    read_independent_source_anchor(root,root/'source',root/'source/anchor.json',anchor,physical_source=candidate)
    return {'status':'prepared','plan_digest':plan['digest'],'candidate':str(candidate),
            'anchor':anchor,'bundle_fingerprint':manifest['fingerprint']}

def exchange_directories(left,right):
    libc=ctypes.CDLL(None,use_errno=True)
    call=getattr(libc,'renameat2',None)
    arguments=(ctypes.c_int(-100),ctypes.c_char_p(os.fsencode(str(left))),ctypes.c_int(-100),ctypes.c_char_p(os.fsencode(str(right))),ctypes.c_uint(2))
    if call is None:
        # Older glibc lacks the wrapper even when the kernel supports renameat2.
        number={'x86_64':316,'aarch64':276}.get(platform.machine())
        if platform.system()!='Linux' or number is None:raise RuntimeError('Atomic directory exchange unavailable; no fallback')
        result=libc.syscall(ctypes.c_long(number),*arguments)
    else:
        call.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        result=call(*arguments)
    if result:
        error=ctypes.get_errno();raise OSError(error,os.strerror(error))
    fd=os.open(str(left.parent),os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def sync_source_tree(bundle,candidate):
    # Persist copied contents and directory entries before exchanging the live
    # anchor. A hash comparison alone does not make dirty pages crash-durable.
    for path in bundle.rglob('*'):
        if path.is_symlink():raise ValueError('Unexpected source bundle link')
        if path.is_file():
            fd=os.open(str(path),os.O_RDONLY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
    directories=[p for p in bundle.rglob('*') if p.is_dir()]+[bundle,bundle.parent,candidate]
    for path in sorted(directories,key=lambda p:len(p.parts),reverse=True):
        fd=os.open(str(path),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)

def switch_source_migration(root,plan,prepared,audit):
    if audit.exists() or audit.is_symlink():raise ValueError('Existing switch audit; inspect, never replay')
    if prepared.get('status')!='prepared' or prepared.get('plan_digest')!=plan['digest']:
        raise ValueError('Prepared source identity changed')
    candidate=root/('.source-next-'+plan['digest'][:16])
    if prepared.get('candidate')!=str(candidate) or candidate.is_symlink() or not candidate.is_dir():
        raise ValueError('Source candidate path changed')
    if source_migration_plan(root)!=plan:raise ValueError('Original source changed before switch')
    anchor=json.loads((candidate/'anchor.json').read_text())
    if anchor!=prepared['anchor']:raise ValueError('Candidate anchor changed')
    read_independent_source_anchor(root,root/'source',root/'source/anchor.json',anchor,physical_source=candidate)
    sync_source_tree(Path(plan['bundle']),candidate)
    report={'status':'switch_requested','plan_digest':plan['digest'],'source':str(root/'source'),
            'candidate':str(candidate),'started_at':time.time()}
    write_new_json(audit,report)
    exchange_directories(root/'source',candidate)
    binding,native,build=read_source_anchor(root)
    if native!=plan['native'] or build!=plan['build']:raise ValueError('Activated source build changed; inspect only')
    # Old manifests are validated against the sealed provenance, not rewritten.
    legacy,_,_=read_source_anchor(root,plan['binding'])
    if legacy!=plan['binding']:raise ValueError('Frozen legacy binding changed')
    if sha(candidate/'anchor.json')!=plan['binding']['sha256']:
        raise ValueError('Exchanged legacy source differs; retain for inspection')
    previous=json.loads((candidate/'anchor.json').read_text())
    if set(p.name for p in candidate.iterdir())!={'anchor.json',*(x['name'] for x in previous['components'])}:
        raise ValueError('Unexpected legacy source entries')
    for item in previous['components']:
        link=candidate/item['name']
        if not link.is_symlink() or os.readlink(str(link))!=item['target']:raise ValueError('Legacy link changed')
    candidate.chmod(0o755)
    for item in previous['components']:(candidate/item['name']).unlink()
    (candidate/'anchor.json').unlink();candidate.rmdir()
    report.update(status='complete',binding=binding,finished_at=time.time())
    temporary=audit.with_suffix('.tmp');write_new_json(temporary,report);os.replace(str(temporary),str(audit))
    fd=os.open(str(audit.parent),os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return report
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['plan','prepare','switch','verify'])
    p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--digest')
    a=p.parse_args();project=Path(__file__).resolve().parents[2]
    if project/'.run' not in a.plan.resolve().parents:raise ValueError('Plan must be inside workspace .run')
    sys.path.insert(0,str(project/'backend_wrf'))
    from config import settings
    from hpc_transport import HpcClient
    import fcntl
    with (project/'.run/wrf-maintenance.lock').open('a') as local_lock:
        fcntl.flock(local_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        header=deployment.VALIDATION+OPERATIONS
        program=header+'\nroot=Path('+repr(settings.hpc_release_root)+')\n'
        if a.action=='plan':
            program+='with (root/"deployment.lock").open("r") as lock:\n fcntl.flock(lock,fcntl.LOCK_SH);print(json.dumps(source_migration_plan(root)))\n'
        else:
            plan=json.loads(a.plan.read_text())
            if a.digest!=plan['digest'] or deployment_digest(plan)!=plan['digest']:raise ValueError('Migration digest mismatch')
            program+='plan=json.loads('+repr(json.dumps(plan))+')\n'
            if a.action=='prepare':body='print(json.dumps(prepare_source_migration(root,plan)))'
            elif a.action=='switch':
                prepared=json.loads(a.plan.with_suffix('.prepared.json').read_text())
                body='print(json.dumps(switch_source_migration(root,plan,json.loads('+repr(json.dumps(prepared))+'),root/"maintenance"/("source-migration-"+plan["digest"]+".json"))))'
            else:body='binding,native,build=read_source_anchor(root);print(json.dumps({"binding":binding,"native_files":len(native),"build":build}))'
            program+='with (root/"deployment.lock").open("a") as lock:\n fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)\n '+body+'\n'
        client=HpcClient(settings)
        command='. '+shlex.quote(settings.hpc_release_root+'/releases/'+settings.hpc_runtime_release+'/runtime.env')+' && '+shlex.quote(settings.hpc_runtime_python)+' -B -c '+shlex.quote(program)
        value=json.loads(client.run(command,timeout=600))
        if a.action=='plan':
            with a.plan.open('x') as output:json.dump(value,output,indent=2)
        else:a.plan.with_suffix('.'+('prepared' if a.action=='prepare' else a.action)+'.json').write_text(json.dumps(value,indent=2))
        print(json.dumps(value if a.action!='plan' else {k:v for k,v in value.items() if k not in {'native','build'}},ensure_ascii=False))


def deployment_digest(plan):
    return hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k!='digest'},sort_keys=True).encode()).hexdigest()


if __name__=='__main__':main()
