"""Fault injection for pool ownership, bounded waits and explicit cleanup."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from config import settings
from forcing_lifecycle import forcing_reference_guard
from hpc_transport import HpcClient, HpcError
from task_manager import WrfTaskManager

SPEC = importlib.util.spec_from_file_location('queue_lifecycle', Path(__file__).parent / 'scripts/forcing_pool_queue.py')
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)
from test_forcing_pool_queue import args


class PoolOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(queue, 'LOCK_ROOT', self.root / 'locks')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pool = queue.PoolQueue(args(self.root / 'pool'))

    def test_missing_or_legacy_owner_is_preserved_not_stolen(self):
        lock = self.pool.worker_lock
        lock.mkdir(parents=True)
        for contents in (None, 'invalid', '99999999'):
            if contents is not None:
                (lock / 'pid').write_text(contents)
            with queue.exclusive_process(lock) as owner:
                self.assertIsNone(owner)
            self.assertTrue(lock.exists())

    def test_inherited_child_descriptor_blocks_replacement_after_parent_releases(self):
        child = None
        try:
            with queue.exclusive_process(self.pool.worker_lock) as fd:
                child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(20)'], pass_fds=(fd,))
            with queue.exclusive_process(self.pool.worker_lock) as other:
                self.assertIsNone(other)
            child.terminate()
            child.wait(timeout=5)
            with queue.exclusive_process(self.pool.worker_lock) as other:
                self.assertIsNotNone(other)
        finally:
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_v2_stale_marker_recovered_only_with_free_kernel_gate(self):
        lock = self.pool.worker_lock
        lock.mkdir(parents=True)
        (lock / 'pid').write_text('99999999')
        (lock / 'owner.json').write_text(json.dumps({'protocol': 'wrf-pool-worker-2', 'root': str(lock.resolve())}))
        with queue.exclusive_process(lock) as owner:
            self.assertIsNotNone(owner)
        self.assertFalse(lock.exists())

    def test_corrupt_queue_is_not_reset(self):
        self.pool.root.mkdir()
        self.pool.state_path.write_text('{invalid')
        with self.assertRaisesRegex(ValueError, 'preserve'):
            self.pool.enqueue('2026100800', [0], 'task')
        self.assertEqual(self.pool.state_path.read_text(), '{invalid')

    def test_cleanup_blocks_worker_pending_task_and_symlink(self):
        cycle = '2026100800'
        path = self.pool.root / cycle
        path.mkdir(parents=True)
        (path / 'input').write_bytes(b'protected')
        with queue.exclusive_process(self.pool.worker_lock):
            self.assertEqual(self.pool.cleanup(cycle), 'BLOCKED')
        self.pool.enqueue(cycle, [0], 'task')
        self.assertEqual(self.pool.cleanup(cycle), 'BLOCKED')
        other = self.pool.root / '2026100700'
        other.symlink_to(path, target_is_directory=True)
        self.assertEqual(self.pool.cleanup('2026100700'), 'BLOCKED')
        self.assertEqual((path / 'input').read_bytes(), b'protected')

    def test_explicit_cleanup_preserves_other_cycles_and_writes_audit(self):
        path = self.pool.root / '2026100800'
        path.mkdir(parents=True)
        (path / 'input').write_bytes(b'discardable fixture')
        keep = self.pool.root / '2026100700'
        keep.mkdir()
        (keep / 'input').write_bytes(b'keep')
        self.assertEqual(self.pool.cleanup('2026100800'), 'DELETED')
        self.assertFalse(path.exists())
        self.assertEqual((keep / 'input').read_bytes(), b'keep')
        audits = list((self.pool.root / 'logs').glob('cleanup_*.json'))
        self.assertEqual(len(audits), 1)
        self.assertEqual(json.loads(audits[0].read_text())['status'], 'deleted')


class DeadlineAndReferenceTest(unittest.TestCase):
    def test_pool_scripts_follow_the_active_immutable_release(self):
        client = HpcClient(replace(settings, hpc_runtime_release='release-fixture', hpc_release_root='/system',
                                   hpc_gfs_download_script='/old/gfs.sh', hpc_ec_download_script='/old/ec.sh'))
        for source in ('gfs', 'ecmwf'):
            self.assertEqual(client._forcing_settings(source)['script'],
                             f'/system/releases/release-fixture/scripts/download_{source}_00z.sh')

    def test_both_sources_expire_without_reenqueue(self):
        for source in ('gfs', 'ecmwf'):
            client = HpcClient(replace(settings, hpc_forcing_wait_timeout_seconds=60))
            client.inspect_gfs_files = client.inspect_forcing_files = Mock(return_value={'complete': False})
            client.trigger_gfs_download = client.trigger_forcing_download = Mock()
            with self.assertRaisesRegex(HpcError, '总时限'):
                client.ensure_remote_forcing(source, '2026100800', [0], deadline_epoch=1)
            client.trigger_gfs_download.assert_not_called()
            client.trigger_forcing_download.assert_not_called()

    def test_wait_budget_is_enforced_for_both_sources_without_wall_clock_progress(self):
        for source in ('gfs', 'ecmwf'):
            client = HpcClient(replace(settings, hpc_forcing_wait_timeout_seconds=60))
            client.inspect_gfs_files = client.inspect_forcing_files = Mock(return_value={'complete': False})
            client.trigger_gfs_download = client.trigger_forcing_download = Mock(return_value={'status': 'queued'})
            client._queue_has_terminal_failure = Mock(return_value=None)
            now = [100.0]
            with patch('hpc_transport.time.monotonic', side_effect=lambda: now[0]), patch('hpc_transport.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
                with self.assertRaisesRegex(HpcError, '总时限'):
                    client.ensure_remote_forcing(source, '2026100800', [0])
            self.assertEqual(now[0], 160.0)

    def test_persisted_deadline_is_not_renewed_on_manager_restart(self):
        task = {'runtime': {}}
        store = Mock()
        store.attempts.return_value = []
        store.get.side_effect = lambda _: task
        store.update.side_effect = lambda _, **fields: task.update(fields) or task
        manager = WrfTaskManager(settings, store, None, Mock())
        first = manager._forcing_wait_deadline('fixture')
        restarted = WrfTaskManager(settings, store, None, Mock())
        self.assertEqual(first, restarted._forcing_wait_deadline('fixture'))
        store.update.assert_called_once()

    def test_cleanup_reads_references_only_after_binding_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(settings, database_path=Path(directory) / 'test.sqlite')
            store, gfs, hpc = Mock(), Mock(), Mock()
            store.forcing_download_cycles.return_value = set()
            store.active.return_value = []
            store.with_status.return_value = []
            manager = WrfTaskManager(cfg, store, gfs, hpc)
            started = threading.Event()
            errors = []
            def cleanup():
                started.set()
                try:
                    manager.cleanup_forcing('gfs', ['/fixture/2026100800'])
                except Exception as exc:
                    errors.append(exc)
            with forcing_reference_guard(cfg.database_path):
                thread = threading.Thread(target=cleanup)
                thread.start()
                self.assertTrue(started.wait(2))
                store.active.return_value = [{'request': {'data_source': 'gfs'}, 'runtime': {'forcing_cycle': '2026100800'}}]
                hpc.cleanup_forcing_cycles.assert_not_called()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(hpc.cleanup_forcing_cycles.call_args.args[-1], {'2026100800'})

    def test_missing_or_duplicate_cleanup_response_is_unknown(self):
        path = '/pool/2026100800'
        client = HpcClient(settings)
        client.gfs_pool_items = Mock(return_value=[{'remote_root': '/pool', 'cleanup_candidates': [path]}])
        for output in ('', f'DELETED|{path}\nMISSING|{path}'):
            client.run = Mock(return_value=output)
            with self.assertRaisesRegex(HpcError, '结果未知'):
                client.cleanup_gfs_cycles([path], [], set())
            client.run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
