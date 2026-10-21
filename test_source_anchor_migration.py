"""Independent-source migration and immutable legacy-binding regressions."""
import json
import os
from pathlib import Path
import shutil
import sys
from unittest import TestCase
from unittest.mock import patch

from scripts import migrate_source_anchor as migration
from scripts import deploy_runtime_release as deployment
import test_thin_runtime_deployment as thin_fixture


class SourceAnchorMigrationTest(TestCase):
    def setUp(self):
        self.fixture=thin_fixture.ThinRuntimeDeploymentTest();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.ns=self.fixture.ns;exec(migration.OPERATIONS,self.ns)
        self.root=self.fixture.fixture.root
        self.plan=self.ns['source_migration_plan'](self.root)
        self.audit=self.root/'migration.json'
    def prepare(self):return self.ns['prepare_source_migration'](self.root,self.plan)
    def switch(self,prepared):
        # macOS unit tests exercise the surrounding transaction; the real atomic
        # exchange is also exercised by the native Linux data-volume run.
        def fixture_exchange(left,right):
            saved=left.with_name('.fixture-exchange');left.rename(saved);right.rename(left);saved.rename(right)
        if sys.platform=='linux':return self.ns['switch_source_migration'](self.root,self.plan,prepared,self.audit)
        with patch.dict(self.ns,exchange_directories=fixture_exchange):
            return self.ns['switch_source_migration'](self.root,self.plan,prepared,self.audit)
    def test_physical_source_copy_and_legacy_release_after_removal(self):
        self.fixture.install();target=self.root/'releases/release-thin';original_manifest=(target/'manifest.json').read_bytes()
        prepared=self.prepare();bundle=Path(self.plan['bundle'])
        self.assertEqual(self.ns['sha'](self.root/'source/anchor.json'),self.plan['binding']['sha256'])
        for folder in ('wrf','wps'):
            for p in (self.fixture.source/folder).rglob('*'):
                if p.is_file():
                    copied=bundle/p.relative_to(self.fixture.source)
                    self.assertEqual(self.ns['sha'](copied),self.ns['sha'](p));self.assertNotEqual(p.stat().st_ino,copied.stat().st_ino)
        result=self.switch(prepared);self.assertEqual(result['status'],'complete')
        self.ns['make_staging_writable'](self.fixture.source);shutil.rmtree(self.fixture.source)
        self.fixture.validate(target,json.loads(original_manifest))
        self.assertEqual((target/'manifest.json').read_bytes(),original_manifest)
        binding,native,build=self.ns['read_source_anchor'](self.root)
        self.assertIn('bundle_id',binding);self.assertNotIn('release_id',binding)
        self.assertEqual(native,self.plan['native']);self.assertEqual(build,self.plan['build'])
        self.assertFalse((self.root/('.source-next-'+self.plan['digest'][:16])).exists())
        with self.assertRaisesRegex(ValueError,'never replay'):self.switch(prepared)
    def test_unknown_frozen_binding_is_not_accepted(self):
        prepared=self.prepare();self.switch(prepared)
        wrong=dict(self.plan['binding'],sha256='0'*64)
        with self.assertRaisesRegex(ValueError,'Unknown frozen source binding'):
            self.ns['read_source_anchor'](self.root,wrong)
    def test_new_thin_release_uses_only_independent_anchor(self):
        prepared=self.prepare();self.switch(prepared)
        self.ns['make_staging_writable'](self.fixture.source);shutil.rmtree(self.fixture.source)
        self.fixture.config['wrf']=str(self.root/'source/wrf-4.7.1')
        self.fixture.config['wps']=str(self.root/'source/wps-4.6.0')
        self.fixture.install();target=self.root/'releases/release-thin'
        manifest=json.loads((target/'manifest.json').read_text());binding=manifest['layout']['source_anchor']
        self.assertEqual(binding['bundle_id'],self.plan['bundle_id']);self.assertNotIn('release_id',binding)
        self.fixture.validate(target,manifest)
    def test_corrupt_source_is_rejected(self):
        prepared=self.prepare();self.switch(prepared)
        path=Path(self.plan['bundle'])/'wrf/phys/unused_source.F'
        path.chmod(0o644);path.write_text('corrupted');path.chmod(0o444)
        with self.assertRaisesRegex(ValueError,'content changed'):self.ns['read_source_anchor'](self.root)
    def test_writable_source_is_rejected(self):
        prepared=self.prepare();self.switch(prepared)
        (Path(self.plan['bundle'])/'wps/README').chmod(0o644)
        with self.assertRaisesRegex(ValueError,'unsafe or writable'):self.ns['read_source_anchor'](self.root)
    def test_failed_atomic_exchange_never_falls_back_or_replays(self):
        prepared=self.prepare();before=(self.root/'source/anchor.json').read_bytes()
        with patch.dict(self.ns,exchange_directories=lambda *args:(_ for _ in ()).throw(OSError('synthetic atomic failure'))):
            with self.assertRaises(OSError):self.ns['switch_source_migration'](self.root,self.plan,prepared,self.audit)
        self.assertEqual((self.root/'source/anchor.json').read_bytes(),before)
        self.assertEqual(json.loads(self.audit.read_text())['status'],'switch_requested')
        with self.assertRaisesRegex(ValueError,'never replay'):self.ns['switch_source_migration'](self.root,self.plan,prepared,self.audit)
    def test_unrecognised_candidate_or_old_source_change_is_rejected(self):
        prepared=self.prepare();candidate=Path(prepared['candidate']);p=candidate/'anchor.json'
        p.chmod(0o644);a=json.loads(p.read_text());a['configuration_source_hash']='0'*64;p.write_text(json.dumps(a));p.chmod(0o444)
        with self.assertRaisesRegex(ValueError,'Candidate anchor changed'):self.switch(prepared)
        self.assertFalse(self.audit.exists());self.assertEqual(self.ns['sha'](self.root/'source/anchor.json'),self.plan['binding']['sha256'])
