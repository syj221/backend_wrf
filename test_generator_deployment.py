"""Generator deployment tests use synthetic files; no service or HPC requests."""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.deploy_namelist_generator import FILES, REMOTE, ordinary_idle, deploy


class GeneratorDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('WRF_GENERATOR_TEST_DIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'ordinary-service'
        self.root.mkdir()
        self.stage = self.root/'.generator-deployments'/('a'*32)
        self.stage.mkdir(parents=True)
        self.old, self.new = {}, {}
        for name in FILES:
            if name != 'namelist_layout.py':
                (self.root/name).write_text('# old synthetic writer\n')
            self.old[name] = hashlib.sha256((self.root/name).read_bytes()).hexdigest() if (self.root/name).exists() else None
            (self.stage/name).write_text('# new synthetic writer\n')
            if name == 'namelist_layout.py':
                (self.stage/name).write_bytes((Path(__file__).parent/'scripts'/name).read_bytes())
            self.new[name] = hashlib.sha256((self.stage/name).read_bytes()).hexdigest()
        (self.root/'wrf.sh').write_text('untouched')
        (self.root/'highres-sentinel').write_text('untouched')

    def install(self):
        return subprocess.run([sys.executable, '-B', '-c', REMOTE, str(self.root), 'install',
            str(self.stage), json.dumps(self.old), json.dumps(self.new)], capture_output=True, text=True)

    def test_installs_dependencies_and_retains_exact_old_backup(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['hashes'], self.new)
        for name in FILES:
            backup = self.stage/'backup'/name
            if self.old[name] is not None:
                self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), self.old[name])
            else:
                self.assertFalse(backup.exists())
        self.assertEqual((self.root/'wrf.sh').read_text(), 'untouched')
        self.assertEqual((self.root/'highres-sentinel').read_text(), 'untouched')
        self.assertEqual(json.loads((self.stage/'deployment.json').read_text())['status'], 'complete')

    def test_concurrent_change_refuses_without_overwriting(self):
        (self.root/'apply_namelist_plan.py').write_text('# changed by another operator')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'namelist_layout.py').exists())
        self.assertFalse((self.stage/'backup').exists())

    def test_corrupt_upload_refuses_before_backup_or_install(self):
        (self.stage/'namelist_layout.py').write_text('# partial transfer')
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.stage/'backup').exists())
        self.assertFalse((self.root/'namelist_layout.py').exists())

    def test_syntax_error_refuses_all_changes(self):
        path = self.stage/'apply_namelist_plan.py'
        path.write_text('def broken(:')
        self.new[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertNotEqual(self.install().returncode, 0)
        self.assertFalse((self.stage/'backup').exists())

    def test_symlink_target_is_not_followed(self):
        outside = Path(self.tmp.name)/'preserve.py'
        outside.write_text('# preserve')
        (self.root/'namelist_layout.py').symlink_to(outside)
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(outside.read_text(), '# preserve')

    def test_partial_install_failure_restores_old_files_and_records_rollback(self):
        wrapper = '''import os
original = os.replace
calls = [0]
def fail_once(source, target):
    calls[0] += 1
    if calls[0] == 2:
        raise OSError('synthetic installation failure')
    return original(source, target)
os.replace = fail_once
exec(%r)
''' % REMOTE
        result = subprocess.run([sys.executable, '-B', '-c', wrapper, str(self.root), 'install',
            str(self.stage), json.dumps(self.old), json.dumps(self.new)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'namelist_layout.py').exists())
        for name in FILES:
            if self.old[name] is not None:
                self.assertEqual(hashlib.sha256((self.root/name).read_bytes()).hexdigest(), self.old[name])
        self.assertEqual(json.loads((self.stage/'deployment.json').read_text())['status'], 'rolled_back')

    def test_immutable_release_refuses_in_place_apply_before_any_io(self):
        client = Mock()
        with patch('scripts.deploy_namelist_generator.ordinary_idle') as idle:
            with self.assertRaisesRegex(RuntimeError, 'deploy_runtime_release.py'):
                deploy(client, SimpleNamespace(hpc_runtime_release='ordinary-frozen'), apply=True)
            idle.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    def test_unknown_or_queued_ordinary_task_prevents_network_and_deploy(self):
        path = Path(self.tmp.name)/'synthetic.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE wrf_tasks (status TEXT)')
            db.execute("INSERT INTO wrf_tasks VALUES ('queued')")
            db.commit()
        with patch('scripts.deploy_namelist_generator.urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(RuntimeError, 'active or uncertain'):
                ordinary_idle(SimpleNamespace(database_path=path))
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
