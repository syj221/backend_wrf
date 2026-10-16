"""Thin native releases preserve the real source probe and wrapper layout."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts import deploy_runtime_release as deploy
from scripts.namelist_source_probe import snapshot
import test_runtime_deployment as runtime_fixture


class ThinRuntimeDeploymentTest(TestCase):
    def setUp(self):
        self.fixture = runtime_fixture.RuntimeDeploymentTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ns = self.fixture.ns
        stage = self.fixture.stage
        self.interpreter = self.fixture.base/'csh'
        self.interpreter.write_text('isolated interpreter fixture')
        self.interpreter.chmod(0o755)
        native_metadata = {
            'wrf/inc/version_decl': "WRF_VERSION = 'V4.7.1'\n",
            'wrf/inc/namelist_statements.inc': 'integer :: max_dom\n',
            'wrf/Registry/Registry.EM_COMMON': 'rconfig integer max_dom namelist,domains 1 1 - "" ""\n',
            'wrf/Registry/registry.physics': 'rconfig integer mp_physics namelist,physics max_domains 8 - "" ""\n',
            'wps/README': 'WRF Preprocessing System Version 4.6.0\n',
            'wps/geogrid/src/gridinfo_module.F': 'module gridinfo_module\nend module\n',
            'wps/metgrid/src/gridinfo_module.F': 'module gridinfo_module\nend module\n',
            'wps/ungrib/src/read_namelist.F': 'subroutine read_namelist\nend\n',
            'wps/namelist.wps.all_options': '&share\n max_dom=1\n/\n',
            'wps/link_grib.csh': '#!'+str(self.interpreter)+' -f\n',
            'wrf/run/RRTMG_LW_DATA': 'runtime radiation table',
            'wrf/run/nested/table.dat': 'runtime nested table',
            'wps/geogrid/GEOGRID.TBL.ARW': 'alternate geography table',
            'wps/metgrid/METGRID.TBL.ARW': 'alternate meteorology table',
            'wps/ungrib/Variable_Tables/Vtable.OTHER': 'other supported conversion table',
            'wrf/Registry/unused_state': 'state real fixture\n',
            'wrf/Registry/excluded.var': 'rconfig integer excluded\n',
            'wrf/phys/unused_source.F': 'large unrelated compiled source fixture\n',
            'wrf/run/namelist.input': 'task configuration excluded by the wrapper',
            'wrf/run/wrfout_d01_fixture': 'task output excluded by the wrapper',
        }
        for relative, value in native_metadata.items():
            path = stage/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        (stage/'wps/link_grib.csh').chmod(0o755)
        self.freeze_source()
        self.source = self.fixture.root/'releases/anchor-full'
        stage.rename(self.source)
        self.source.chmod(0o555)
        (self.fixture.root/'deployment.lock').touch()
        with patch('sys.argv', ['anchor',str(self.fixture.root),'anchor-full','apply']), \
                patch('subprocess.run',side_effect=self.fixture.run_command), redirect_stdout(io.StringIO()):
            exec(deploy.ANCHOR_SOURCE,{})
        self.stage = self.fixture.root/'staging'/('b'*32)
        shutil.copytree(self.source/'scripts', self.stage/'scripts')
        self.config = {
            'release_id':'release-thin', 'wrf':str(self.source/'wrf'), 'wps':str(self.source/'wps'),
            'geog':str(self.fixture.geog), 'python':str(self.fixture.python), 'minimum_free_bytes':0,
            'environment':{'PATH':'/usr/bin:/bin','ECCODES_DIR':str(self.fixture.eccodes)},
            'scripts':{path.name:self.ns['sha'](path) for path in (self.stage/'scripts').iterdir()},
            'thin':True,
        }

    def freeze_source(self):
        stage = self.fixture.stage
        self.fixture.manifest['release_id'] = 'anchor-full'
        build = snapshot(stage/'wrf', stage/'wps', stage/'scripts')
        self.fixture.manifest['build'] = {key:build[key] for key in ('source_hash','binary_hashes','generator_hashes')}
        self.fixture.manifest['files'] = {str(path.relative_to(stage)):self.ns['sha'](path)
                                         for path in stage.rglob('*') if path.is_file()}
        self.fixture.seal()
        self.ns['make_readonly'](stage)

    def install(self):
        with patch('sys.argv', ['deploy',str(self.fixture.root),str(self.fixture.base/'data'),
                                str(self.stage),json.dumps(self.config)]), \
                patch('subprocess.run',side_effect=self.fixture.run_command), redirect_stdout(io.StringIO()) as output, \
                patch('sys.path',list(__import__('sys').path)):
            exec(deploy.REMOTE,{})
        return json.loads(output.getvalue())

    def validate(self, target, manifest):
        with patch('subprocess.run',side_effect=self.fixture.run_command):
            self.ns['validate_release'](target,manifest,'release-thin')

    def test_native_copy_preserves_build_and_wrapper_files_without_source_copy_or_links(self):
        before = {str(path.relative_to(self.source)):(self.ns['sha'](path),path.stat().st_mode)
                  for path in self.source.rglob('*') if path.is_file()}
        expected = snapshot(self.source/'wrf',self.source/'wps',self.source/'scripts')
        result = self.install()
        target = self.fixture.root/'releases/release-thin'
        manifest = json.loads((target/'manifest.json').read_text())
        actual = snapshot(target/'wrf',target/'wps',target/'scripts')
        self.assertEqual(result['status'],'installed')
        self.assertEqual(manifest['layout']['mode'],'thin')
        self.assertEqual(manifest['layout']['source_anchor']['release_id'],'anchor-full')
        for key in ('source_hash','binary_hashes','generator_hashes','wrf_version','wps_version'):
            self.assertEqual(actual[key],expected[key],key)
        for relative in self.ns['PROGRAMS']+self.ns['TABLES']+self.ns['NATIVE_METADATA']+(
                'wps/link_grib.csh','wrf/run/RRTMG_LW_DATA','wrf/run/nested/table.dat',
                'wps/ungrib/Variable_Tables/Vtable.OTHER','wps/geogrid/GEOGRID.TBL.ARW',
                'wps/metgrid/METGRID.TBL.ARW'):
            self.assertTrue((target/relative).is_file(),relative)
        for relative in ('wrf/phys','wrf/Registry/unused_state','wrf/Registry/excluded.var',
                         'wrf/run/namelist.input','wrf/run/wrfout_d01_fixture'):
            self.assertFalse((target/relative).exists(),relative)
        self.assertEqual({path.name for path in (target/'scripts').iterdir()},set(deploy.SCRIPTS))
        self.assertIn(str(self.interpreter),manifest['dependencies'])
        for path in [target]+list(target.rglob('*')):
            self.assertFalse(path.is_symlink(),path)
            self.assertEqual(path.stat().st_mode & 0o222,0,path)
        for relative in self.ns['PROGRAMS']+('wps/link_grib.csh',):
            self.assertEqual((target/relative).stat().st_mode & 0o111,0o111)
        self.validate(target,manifest)
        self.assertEqual(before,{str(path.relative_to(self.source)):(self.ns['sha'](path),path.stat().st_mode)
                                 for path in self.source.rglob('*') if path.is_file()})

    def test_missing_source_table_and_metadata_are_rejected_before_activation(self):
        alternate = self.fixture.base/'alternate'
        shutil.copytree(self.source,alternate)
        self.ns['make_staging_writable'](alternate)
        self.config['wrf'] = str(alternate/'wrf')
        self.config['wps'] = str(alternate/'wps')
        table = alternate/'wrf/run/RRTMG_LW_DATA'
        table.unlink()
        with self.assertRaisesRegex(ValueError,'differs from fixed source anchor'):
            self.install()
        self.assertEqual((self.fixture.root/'current').resolve(),self.fixture.old)
        table.write_bytes((self.source/'wrf/run/RRTMG_LW_DATA').read_bytes())
        (alternate/'wrf/inc/namelist_statements.inc').unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()
        self.assertFalse((self.fixture.root/'releases/release-thin').exists())

    def test_escaping_dangling_and_directory_links_in_selected_sources_are_rejected(self):
        alternate = self.fixture.base/'alternate'
        shutil.copytree(self.source,alternate)
        self.ns['make_staging_writable'](alternate)
        self.config['wrf'] = str(alternate/'wrf')
        self.config['wps'] = str(alternate/'wps')
        path = alternate/'wrf/run/LANDUSE.TBL'
        original = path.read_bytes()
        path.unlink();path.symlink_to(self.fixture.library)
        with self.assertRaisesRegex(ValueError,'Unsafe thin source file'):
            self.install()
        path.unlink();path.symlink_to(alternate/'wrf/missing-table')
        with self.assertRaises(FileNotFoundError):
            self.install()
        path.unlink();path.write_bytes(original)
        (alternate/'wrf/run/link-directory').symlink_to(alternate/'wrf/phys',target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'directory link is unsupported'):
            self.install()
        self.assertEqual((self.fixture.root/'current').resolve(),self.fixture.old)

    def test_internal_source_file_links_are_materialized(self):
        alternate = self.fixture.base/'alternate'
        shutil.copytree(self.source,alternate)
        self.ns['make_staging_writable'](alternate)
        self.config['wrf'] = str(alternate/'wrf')
        self.config['wps'] = str(alternate/'wps')
        path = alternate/'wrf/run/RRTMG_LW_DATA'
        target = alternate/'wrf/phys/compiled-table'
        target.write_bytes(path.read_bytes())
        path.unlink();path.symlink_to(target)
        self.install()
        copied = self.fixture.root/'releases/release-thin/wrf/run/RRTMG_LW_DATA'
        self.assertFalse(copied.is_symlink())
        self.assertEqual(copied.read_bytes(),target.read_bytes())

    def test_missing_script_prevents_first_validation(self):
        (self.stage/'scripts').chmod(0o755)
        (self.stage/'scripts/output_schedule.py').unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()
        self.assertEqual((self.fixture.root/'current').resolve(),self.fixture.old)
        self.assertFalse((self.stage/'manifest.json').exists())

    def test_nonexecutable_linker_prevents_first_validation(self):
        alternate = self.fixture.base/'alternate'
        shutil.copytree(self.source,alternate)
        self.ns['make_staging_writable'](alternate)
        self.config['wrf'] = str(alternate/'wrf')
        self.config['wps'] = str(alternate/'wps')
        (alternate/'wps/link_grib.csh').chmod(0o644)
        with self.assertRaisesRegex(ValueError,'GRIB linker is not executable'):
            self.install()
        self.assertEqual((self.fixture.root/'current').resolve(),self.fixture.old)
        self.assertFalse((self.stage/'manifest.json').exists())

    def test_linker_interpreter_and_its_dependencies_are_revalidated(self):
        self.install()
        target = self.fixture.root/'releases/release-thin'
        manifest = json.loads((target/'manifest.json').read_text())
        self.interpreter.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'interpreter is not executable'):
            self.validate(target,manifest)
        self.interpreter.chmod(0o755)
        self.interpreter.write_text('changed interpreter')
        with self.assertRaisesRegex(ValueError,'Fixed dependency changed'):
            self.validate(target,manifest)

    def test_retained_thin_transaction_uses_the_same_validator_and_seal(self):
        self.install()
        target = self.fixture.root/'releases/release-thin'
        manifest = json.loads((target/'manifest.json').read_text())
        (self.fixture.root/'current').unlink()
        (self.fixture.root/'current').symlink_to(self.fixture.old)
        target.chmod(0o755);target.rename(self.stage)
        self.ns['make_staging_writable'](self.stage)
        extra = self.stage/'scripts/alias'
        extra.symlink_to('wrf.sh')
        with patch('sys.argv',['resume',str(self.fixture.root),str(self.stage),'release-thin']), \
                patch('subprocess.run',side_effect=self.fixture.run_command), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'links are prohibited'):
                exec(deploy.RESUME,{})
        self.assertEqual((self.fixture.root/'current').resolve(),self.fixture.old)
        extra.unlink()
        with patch('sys.argv',['resume',str(self.fixture.root),str(self.stage),'release-thin']), \
                patch('subprocess.run',side_effect=self.fixture.run_command), redirect_stdout(io.StringIO()):
            exec(deploy.RESUME,{})
        self.assertEqual((self.fixture.root/'current').resolve(),target)
        self.assertEqual(json.loads((target/'manifest.json').read_text()),manifest)
        self.assertEqual(target.stat().st_mode & 0o222,0)
        self.validate(target,manifest)

    def test_thin_layout_cannot_be_used_as_authoritative_full_source(self):
        self.install()
        with patch('sys.argv',['anchor',str(self.fixture.root),'release-thin','plan']), \
                patch('subprocess.run',side_effect=self.fixture.run_command), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'cannot supply authoritative source'):
                exec(deploy.ANCHOR_SOURCE,{})

    def test_resume_rejects_omitted_table_even_with_recomputed_manifest_digests(self):
        self.install()
        target = self.fixture.root/'releases/release-thin'
        target.chmod(0o755);target.rename(self.stage)
        self.ns['make_staging_writable'](self.stage)
        manifest = json.loads((self.stage/'manifest.json').read_text())
        relative = 'wrf/run/RRTMG_LW_DATA'
        (self.stage/relative).unlink()
        manifest['files'].pop(relative)
        manifest['layout']['native_files'].pop(relative)
        manifest['layout']['native_inventory_sha256'] = self.ns['document_digest'](manifest['layout']['native_files'])
        manifest['validation']['content_sha256'] = self.ns['document_digest'](self.ns['release_content'](manifest))
        manifest['fingerprint'] = self.ns['document_digest']({key:value for key,value in manifest.items() if key!='fingerprint'})
        (self.stage/'manifest.json').write_text(json.dumps(manifest))
        with patch('sys.argv',['resume',str(self.fixture.root),str(self.stage),'release-thin']), \
                patch('subprocess.run',side_effect=self.fixture.run_command), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,'differs from fixed source anchor'):
                exec(deploy.RESUME,{})
        self.assertFalse(target.exists())

    def test_original_source_anchor_script_inventory_does_not_require_new_release_scripts(self):
        source_manifest_path = self.source/'manifest.json'
        self.ns['make_staging_writable'](self.source)
        manifest = json.loads(source_manifest_path.read_text())
        manifest.pop('geography')
        for name in ('output_schedule.py','validate_geography.py'):
            (self.source/'scripts'/name).unlink()
            manifest['files'].pop('scripts/'+name)
        manifest['validation']['content_sha256'] = self.ns['document_digest'](self.ns['release_content'](manifest))
        manifest['fingerprint'] = self.ns['document_digest']({key:value for key,value in manifest.items() if key!='fingerprint'})
        source_manifest_path.write_text(json.dumps(manifest))
        self.ns['make_readonly'](self.source);self.source.chmod(0o555)
        anchor_path = self.fixture.root/'source/anchor.json'
        anchor = json.loads(anchor_path.read_text())
        anchor['release_manifest_sha256'] = self.ns['sha'](source_manifest_path)
        anchor['release_fingerprint'] = manifest['fingerprint']
        anchor_path.chmod(0o644);anchor_path.write_text(json.dumps(anchor));anchor_path.chmod(0o444)
        self.install()
        target = self.fixture.root/'releases/release-thin'
        self.assertTrue((target/'scripts/output_schedule.py').is_file())
        self.validate(target,json.loads((target/'manifest.json').read_text()))

    def test_plan_is_readonly_and_full_remains_the_default(self):
        settings = SimpleNamespace(hpc_wrf_source_dir='/fixture/wrf',hpc_wps_source_dir='/fixture/wps',
                                   hpc_geog_dir='/fixture/geog',hpc_runtime_min_free_gb=0)
        client = Mock()
        arguments = (client,settings,str(self.fixture.root),str(self.fixture.base/'data'),str(self.fixture.python),'new')
        full = deploy.deploy(*arguments)
        thin = deploy.deploy(*arguments,thin=True)
        self.assertNotIn('thin',full['config'])
        self.assertTrue(thin['config']['thin'])
        client.run.assert_not_called();client.upload.assert_not_called()
