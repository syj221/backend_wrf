"""Source anchors reuse sealed synthetic trees without SSH or computation."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock,patch

from scripts import deploy_runtime_release as deploy
from scripts.namelist_source_probe import snapshot
import test_runtime_deployment as runtime_fixture


class SourceAnchorTest(TestCase):
    def setUp(self):
        self.fixture = runtime_fixture.RuntimeDeploymentTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        stage = self.fixture.stage
        (stage/'wrf/inc').mkdir(parents=True)
        (stage/'wrf/inc/version_decl').write_text("WRF_VERSION = 'V4.7.1'\n")
        (stage/'wps/README').write_text('WRF Preprocessing System Version 4.6.0\n')
        self.freeze()
        self.release = self.fixture.root/'releases'/'release-fixture'
        stage.chmod(0o755)
        stage.rename(self.release)
        self.release.chmod(0o555)
        (self.fixture.root/'deployment.lock').touch()

    def freeze(self):
        stage = self.fixture.stage
        build = snapshot(stage/'wrf',stage/'wps',stage/'scripts')
        self.fixture.manifest['build'] = {key:build[key] for key in ('source_hash','binary_hashes','generator_hashes')}
        self.fixture.manifest['files'] = {str(path.relative_to(stage)):self.fixture.ns['sha'](path)
                                         for path in stage.rglob('*') if path.is_file() and path.name != 'manifest.json'}
        self.fixture.seal()
        self.fixture.ns['make_readonly'](stage)
        stage.chmod(0o555)

    def run_anchor(self,apply=False):
        with patch('sys.argv',['anchor',str(self.fixture.root),'release-fixture','apply' if apply else 'plan']), \
                patch('subprocess.run',side_effect=self.fixture.run_command),redirect_stdout(io.StringIO()) as output:
            exec(deploy.ANCHOR_SOURCE,{})
        return json.loads(output.getvalue())

    def test_plan_verifies_without_creating_a_source_entry(self):
        old_current = (self.fixture.root/'current').resolve()
        result = self.run_anchor()
        self.assertEqual(result['status'],'ready_to_anchor')
        self.assertEqual(result['physical_source_bytes_copied'],0)
        self.assertFalse((self.fixture.root/'source').exists())
        self.assertEqual((self.fixture.root/'current').resolve(),old_current)

    def test_apply_pins_readonly_trees_and_is_idempotent_after_current_changes(self):
        result = self.run_anchor(apply=True)
        source = self.fixture.root/'source'
        marker = source/'anchor.json'
        original = {path.name:path.lstat().st_ino for path in source.iterdir()}
        self.assertEqual(result['status'],'anchored')
        self.assertEqual(marker.stat().st_mode & 0o222,0)
        self.assertEqual(source.stat().st_mode & 0o222,0)
        anchor = json.loads(marker.read_text())
        self.assertEqual(anchor['protected_release_paths'],[str(self.release)])
        for name,folder in [('wrf-4.7.1','wrf'),('wps-4.6.0','wps')]:
            self.assertTrue((source/name).is_symlink())
            self.assertEqual((source/name).resolve(),self.release/folder)
        (self.fixture.root/'current').unlink()
        (self.fixture.root/'current').symlink_to(self.fixture.old)
        again = self.run_anchor(apply=True)
        self.assertEqual(again['status'],'already_anchored')
        self.assertEqual({path.name:path.lstat().st_ino for path in source.iterdir()},original)

    def test_existing_foreign_path_is_never_replaced(self):
        source = self.fixture.root/'source'
        source.mkdir()
        protected = source/'foreign'
        protected.write_text('keep')
        for apply in (False,True):
            with self.assertRaisesRegex(ValueError,'no overwrite'):
                self.run_anchor(apply)
            self.assertEqual(protected.read_text(),'keep')

    def test_existing_anchor_drift_and_unsealed_source_are_rejected(self):
        self.run_anchor(apply=True)
        source = self.fixture.root/'source'
        source.chmod(0o755)
        with self.assertRaisesRegex(ValueError,'not sealed'):
            self.run_anchor()
        source.chmod(0o555)
        marker = source/'anchor.json'
        marker.chmod(0o644)
        value = json.loads(marker.read_text())
        value['protected_release_paths'] = []
        marker.write_text(json.dumps(value))
        marker.chmod(0o444)
        with self.assertRaisesRegex(ValueError,'manifest differs'):
            self.run_anchor(apply=True)

    def test_source_hash_drift_refuses_before_creating_the_entry(self):
        path = self.release/'wrf/inc/version_decl'
        path.chmod(0o644)
        path.write_text("WRF_VERSION = 'V4.7.10'\n")
        path.chmod(0o444)
        with self.assertRaisesRegex(ValueError,'Retained file changed'):
            self.run_anchor(apply=True)
        self.assertFalse((self.fixture.root/'source').exists())

    def test_version_mismatch_is_rejected_even_with_a_new_valid_seal(self):
        self.release.chmod(0o755)
        self.release.rename(self.fixture.stage)
        self.fixture.ns['make_staging_writable'](self.fixture.stage)
        (self.fixture.stage/'wrf/inc/version_decl').write_text("WRF_VERSION = 'V4.7.10'\n")
        self.freeze()
        self.fixture.stage.chmod(0o755)
        self.fixture.stage.rename(self.release)
        self.release.chmod(0o555)
        with self.assertRaisesRegex(ValueError,'Source version differs'):
            self.run_anchor()
        self.assertFalse((self.fixture.root/'source').exists())

    def test_shell_and_release_path_inputs_are_checked_before_ssh(self):
        client = Mock()
        for root,python,release in [('/root;false','/python','release'),('/root','/python','../release')]:
            with self.assertRaises(ValueError):
                deploy.anchor_source(client,root,python,release)
        client.run.assert_not_called()
