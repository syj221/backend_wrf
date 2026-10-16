"""Release recovery gates in private fixtures; no SSH or WRF execution."""
import copy
import io
import json
import os
import shutil
import shlex
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts import deploy_runtime_release as deploy
from scripts.namelist_source_probe import snapshot
from test_geography_validation import geography_fixture, TABLE
from scripts.validate_geography import validate_geography
from runtime_release import PROBE


class RuntimeDeploymentTest(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='wrf-release-test-')
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'system'
        self.stage = self.root / 'staging' / ('a' * 32)
        self.stage.mkdir(parents=True)
        for name in ('releases', 'manifests'):
            (self.root / name).mkdir()
        self.old = self.root / 'releases' / 'previous'
        self.old.mkdir()
        (self.root / 'current').symlink_to(self.old)
        self.ns = {}
        exec(deploy.VALIDATION, self.ns)
        self.python = self.base / 'python'
        self.python.write_text('isolated Python fixture')
        self.python.chmod(0o755)
        self.library = self.base / 'library.so'
        self.library.write_text('isolated shared library fixture')
        self.eccodes = self.base / 'eccodes'
        (self.eccodes / 'bin').mkdir(parents=True)
        (self.eccodes / 'lib').mkdir()
        self.grib_filter = self.eccodes / 'bin/grib_filter'
        self.grib_filter.write_text('isolated ecCodes CLI fixture')
        self.grib_filter.chmod(0o755)
        (self.eccodes / 'lib/libeccodes.so').symlink_to(self.library)
        self.packages = {'f90nml': '1.4.4', 'netCDF4': '1.7.2', 'eccodes': '2.39.1'}
        self.geog = geography_fixture(self.base)
        for name in self.ns['PROGRAMS'] + self.ns['TABLES'] + ('runtime.env',):
            path = self.stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture')
            if name in self.ns['PROGRAMS']:
                path.chmod(0o755)
        for name in deploy.SCRIPTS:
            path = self.stage / 'scripts' / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('fixture')
        for name in ('namelist_source_probe.py', 'validate_geography.py'):
            (self.stage / 'scripts' / name).write_bytes(
                (Path(__file__).parent / 'scripts' / name).read_bytes())
        (self.stage/'wps/geogrid/GEOGRID.TBL').write_text(TABLE)
        build = snapshot(self.stage / 'wrf', self.stage / 'wps', self.stage / 'scripts')
        self.manifest = {
            'schema': 'wrf-runtime-release-1', 'release_id': 'release-fixture',
            'geog_path': str(self.geog), 'minimum_free_bytes': 0, 'python': str(self.python),
            'runtime_tools': {'grib_filter': str(self.grib_filter)},
            'geography': validate_geography(self.stage/'wps/geogrid/GEOGRID.TBL', self.geog),
            'files': {str(p.relative_to(self.stage)): self.ns['sha'](p)
                      for p in self.stage.rglob('*') if p.is_file()},
            'dependencies': {str(p.resolve()): self.ns['sha'](p) for p in (self.python, self.library, self.grib_filter)},
            'python_packages': self.packages,
            'build': {k: build[k] for k in ('source_hash', 'binary_hashes', 'generator_hashes')},
        }

    def tearDown(self):
        # Successful recovery seals the fixture. Restore fixture permissions
        # solely so TemporaryDirectory can remove its own isolated contents.
        for base, directories, files in os.walk(self.base):
            Path(base).chmod(0o755)
            for name in files:
                path = Path(base) / name
                if not path.is_symlink():
                    path.chmod(0o644)
        self.temporary.cleanup()

    def run_command(self, args, **kwargs):
        command = args[2]
        if 'importlib.metadata' in command:
            output = json.dumps({'packages': self.packages, 'dependencies': [str(self.library)]})
        else:
            output = 'libfixture.so => ' + str(self.library) + ' (0x0000)\n'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    def seal(self):
        with patch('subprocess.run', side_effect=self.run_command):
            self.ns['complete_validation'](self.stage, self.manifest)

    def resume(self, release_id='release-fixture'):
        with patch('sys.argv', ['resume', str(self.root), str(self.stage), release_id]), \
                patch('subprocess.run', side_effect=self.run_command), redirect_stdout(io.StringIO()) as output:
            exec(deploy.RESUME, {})
        return json.loads(output.getvalue())

    def assert_unchanged(self):
        self.assertEqual((self.root / 'current').resolve(), self.old)
        self.assertTrue(self.stage.is_dir())
        self.assertFalse((self.root / 'releases' / 'release-fixture').exists())

    def geography_readiness(self):
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/proc/meminfo': return 'MemAvailable: 9999999 kB\n'
            return read_text(path, *args, **kwargs)
        process = SimpleNamespace(returncode=0, communicate=lambda: (b'fixture ldd', None))
        with patch('sys.argv', ['probe',str(self.stage),str(self.base),json.dumps(self.manifest['build'])]), \
                patch.object(Path, 'read_text', read), patch('subprocess.Popen',return_value=process), \
                redirect_stdout(io.StringIO()) as output:
            exec(PROBE,{})
        report = json.loads(output.getvalue())
        return next(check for check in report['checks'] if check['code']=='geography')

    def test_geography_is_rechecked_on_first_activation_resume_and_readiness(self):
        self.seal()
        self.assertTrue(self.geography_readiness()['ok'])
        (self.geog/'global/00001-00002.00001-00002').unlink()
        self.assertFalse(self.geography_readiness()['ok'])
        with self.assertRaisesRegex(ValueError,'tile inventory differs'): self.resume()
        self.manifest.pop('validation');self.manifest.pop('fingerprint')
        with patch('subprocess.run', side_effect=self.run_command):
            with self.assertRaisesRegex(ValueError,'tile inventory differs'):
                self.ns['complete_validation'](self.stage,self.manifest)
        self.assertNotIn('validation',self.manifest)
        self.assert_unchanged()

    def test_readiness_does_not_execute_an_unpinned_geography_helper(self):
        self.seal()
        helper = self.stage/'scripts/validate_geography.py'
        helper.write_text("raise AssertionError('untrusted helper was executed')\n")
        result = self.geography_readiness()
        self.assertFalse(result['ok'])
        self.assertIn('not pinned',result['detail'])

    def test_legacy_04_manifest_keeps_its_original_script_and_geography_contract(self):
        self.manifest.pop('geography')
        for name in ('output_schedule.py','validate_geography.py'):
            (self.stage/'scripts'/name).unlink()
            self.manifest['files'].pop('scripts/'+name)
        self.seal()
        # Old immutable manifests are not silently upgraded by local changes.
        (self.geog/'global/00001-00002.00001-00002').unlink()
        self.assertTrue(self.geography_readiness()['ok'])
        self.assertEqual(self.resume()['status'],'installed')

    def write_manifest(self, manifest):
        (self.stage / 'manifest.json').write_text(json.dumps(manifest))

    def test_resume_rejects_prevalidation_and_old_manifest(self):
        self.write_manifest(self.manifest)
        with self.assertRaisesRegex(ValueError, 'complete validation marker'):
            self.resume()
        self.assert_unchanged()
        self.write_manifest({'schema': 'invalid', 'release_id': 'release-fixture',
                             'fingerprint': 'not-a-sha256', 'files': {}})
        with self.assertRaisesRegex(ValueError, 'manifest schema'):
            self.resume()
        self.assert_unchanged()

    def test_resume_revalidates_all_gates_then_reseals_before_switch(self):
        self.seal()
        self.assertTrue(self.manifest['validation']['complete'])
        self.assertEqual(self.manifest['validation']['checks'], list(deploy.VALIDATION_CHECKS))
        result = self.resume()
        target = self.root / 'releases' / 'release-fixture'
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.root / 'current').resolve(), target)
        self.assertFalse(self.stage.exists())
        for path in target.rglob('*'):
            if not path.is_symlink():
                self.assertEqual(path.stat().st_mode & 0o222, 0, path)
        self.assertEqual((target / 'wrf/main/wrf.exe').stat().st_mode & 0o111, 0o111)
        self.assertTrue((self.root / 'manifests/release-release-fixture.json').is_file())

    def test_resume_rejects_marker_or_manifest_mutation(self):
        self.seal()
        for field, value, message in (
            ('fingerprint', '0' * 64, 'fingerprint'),
            ('python', '/changed/python', 'validation content'),
            ('validation', {'complete': True}, 'complete validation marker'),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.manifest)
                changed[field] = value
                self.write_manifest(changed)
                with self.assertRaisesRegex(ValueError, message):
                    self.resume()
                self.assert_unchanged()

    def test_resume_rejects_runtime_drift_and_extra_unfrozen_files(self):
        self.seal()
        self.library.write_text('changed dependency')
        with self.assertRaisesRegex(ValueError, 'Fixed dependency changed'):
            self.resume()
        self.assert_unchanged()
        self.library.write_text('isolated shared library fixture')
        self.packages = {**self.packages, 'unfrozen-package': '1.0'}
        with self.assertRaisesRegex(ValueError, 'Python package inventory changed'):
            self.resume()
        self.assert_unchanged()
        self.packages.pop('unfrozen-package')
        (self.stage / 'extra.py').write_text('unfrozen')
        with self.assertRaisesRegex(ValueError, 'outside the frozen inventory'):
            self.resume()
        self.assert_unchanged()

    def test_resume_rejects_missing_library_even_with_intact_file_hashes(self):
        self.seal()
        def missing(args, **kwargs):
            if 'ldd ' in args[2]:
                return SimpleNamespace(returncode=0, stdout='libfixture.so => not found\n', stderr='')
            return self.run_command(args, **kwargs)
        with patch('subprocess.run', side_effect=missing):
            with self.assertRaisesRegex(ValueError, 'Binary dependency gate failed'):
                self.ns['validate_release'](self.stage, self.manifest)
        self.assert_unchanged()

    def test_first_activation_has_no_complete_marker_when_a_gate_fails(self):
        self.manifest['build']['generator_hashes']['wrf.sh'] = '0' * 64
        with patch('subprocess.run', side_effect=self.run_command):
            with self.assertRaisesRegex(ValueError, 'build contract changed'):
                self.ns['complete_validation'](self.stage, self.manifest)
        self.assertNotIn('validation', self.manifest)
        self.assertFalse((self.stage / 'manifest.json').exists())
        self.assert_unchanged()

    def test_resume_rejects_wrong_release_and_escaping_link(self):
        self.seal()
        with self.assertRaisesRegex(ValueError, 'release ID differs'):
            self.resume('another-release')
        self.assert_unchanged()
        (self.stage / 'escape').symlink_to(self.library)
        with self.assertRaisesRegex(ValueError, 'link escapes'):
            self.resume()
        self.assert_unchanged()

    def test_fixed_grib_filter_must_be_executable_and_fingerprinted(self):
        self.seal()
        self.grib_filter.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'Missing executable'):
            self.resume()
        self.assert_unchanged()
        self.grib_filter.chmod(0o755)
        self.grib_filter.write_text('changed ecCodes CLI')
        with self.assertRaisesRegex(ValueError, 'Fixed dependency changed'):
            self.resume()
        self.assert_unchanged()

    def test_staging_write_access_never_follows_links_to_original_files(self):
        self.library.chmod(0o444)
        (self.stage / 'external-library-link').symlink_to(self.library)
        self.ns['make_readonly'](self.stage)
        with patch('os.chmod', side_effect=NotImplementedError('remote chmod no-follow unsupported')):
            self.ns['make_staging_writable'](self.stage)
        self.assertEqual(self.library.stat().st_mode & 0o222, 0)
        self.assertTrue((self.stage / 'external-library-link').is_symlink())
        self.assertEqual((self.stage / 'wrf/main/wrf.exe').stat().st_mode & 0o200, 0o200)

    def test_first_deployment_copies_a_readonly_release_and_keeps_source_immutable(self):
        source = self.root / 'releases/source-readonly'
        shutil.copytree(self.stage, source)
        self.ns['make_readonly'](source)
        source.chmod(0o555)
        original = {str(p.relative_to(source)): (self.ns['sha'](p), p.stat().st_mode)
                    for p in source.rglob('*') if p.is_file()}
        stage = self.root / 'staging' / ('b' * 32)
        shutil.copytree(source / 'scripts', stage / 'scripts')
        config = {
            'release_id': 'release-first', 'wrf': str(source / 'wrf'),
            'wps': str(source / 'wps'), 'geog': str(self.geog),
            'python': str(self.python), 'minimum_free_bytes': 0,
            'environment': {'PATH': '/usr/bin:/bin', 'ECCODES_DIR': str(self.eccodes)},
            'scripts': {p.name: self.ns['sha'](p) for p in (stage / 'scripts').iterdir()},
        }
        with patch('sys.argv', ['deploy', str(self.root), str(self.base / 'data'),
                                str(stage), json.dumps(config)]), \
                patch('subprocess.run', side_effect=self.run_command), redirect_stdout(io.StringIO()) as output, \
                patch('sys.path', list(__import__('sys').path)):
            exec(deploy.REMOTE, {})
        result = json.loads(output.getvalue())
        target = self.root / 'releases/release-first'
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.root / 'current').resolve(), target)
        manifest = json.loads((target / 'manifest.json').read_text())
        self.assertTrue(manifest['validation']['complete'])
        self.assertIn(str(self.library), manifest['dependencies'])
        self.assertIn(str(self.grib_filter), manifest['dependencies'])
        self.assertEqual(manifest['runtime_tools'], {'grib_filter': str(self.grib_filter)})
        self.assertIn('export WRF_GRIB_FILTER=' + str(self.grib_filter), (target / 'runtime.env').read_text())
        with patch('subprocess.run', side_effect=self.run_command):
            self.ns['validate_release'](target, manifest, 'release-first')
        self.assertEqual(original, {str(p.relative_to(source)): (self.ns['sha'](p), p.stat().st_mode)
                                    for p in source.rglob('*') if p.is_file()})
        self.assertEqual(source.stat().st_mode & 0o222, 0)

    def test_deploy_and_resume_use_the_configured_python_instead_of_shell_python(self):
        client = Mock()
        client.run.side_effect = [json.dumps({'PATH': '/usr/bin:/bin'}), '', json.dumps({'status': 'installed'})]
        settings = SimpleNamespace(hpc_wrf_source_dir='/fixture/wrf',hpc_wps_source_dir='/fixture/wps',
                                   hpc_geog_dir='/fixture/geog',hpc_runtime_min_free_gb=0,
                                   hpc_runtime_env='/fixture/runtime.env')
        self.assertEqual(deploy.deploy(client,settings,str(self.root),str(self.base/'data'),
                                      str(self.python),'release-fixture',apply=True)['status'],'installed')
        self.assertEqual(shlex.split(client.run.call_args.args[0])[0],str(self.python))
        client.run.return_value=json.dumps({'status':'installed'});client.run.side_effect=None
        self.assertEqual(deploy.resume(client,str(self.root),'a'*32,str(self.python),'release-fixture')['status'],'installed')
        self.assertEqual(shlex.split(client.run.call_args.args[0])[0],str(self.python))
