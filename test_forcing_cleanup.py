"""Cleanup plans bind the operator, current references and exact remote bytes."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
import threading
import time

from database import TaskStore
from forcing_cleanup import ForcingCleanupService
from scripts.forcing_pool_queue import PoolQueue
from hpc_transport import HpcClient, HpcError
from test_pool_resilience import queue_args


class ForcingCleanupTest(unittest.TestCase):
    def test_fingerprint_does_not_hold_reference_lock_and_new_reference_blocks_plan(self):
        from forcing_lifecycle import forcing_reference_guard
        calls=0
        original=self.manager.hpc.snapshot_forcing_cycles
        def fingerprint(*args):
            nonlocal calls
            with forcing_reference_guard(self.store.path,timeout=.1):
                calls+=1
            self.manager.hpc.forcing_pool_items.return_value=[{'cycles':[]}]
            return original.return_value
        self.manager.hpc.snapshot_forcing_cycles.side_effect=fingerprint
        with self.assertRaisesRegex(ValueError,'核验期间变化'):
            self.service.plan('gfs','alice',directory_mode=False)
        self.assertEqual(calls,1)

    def test_snapshot_accepts_only_configured_root_and_its_canonical_target(self):
        import json
        client=object.__new__(HpcClient)
        client.settings=SimpleNamespace(hpc_runtime_python='/ordinary/python')
        client._forcing_settings=Mock(return_value={'root':'/alias/forcing/gfs','script':'/release/scripts/download.sh'})
        entry={'cycle':'2026100100','path':'/mounted/forcing/gfs/2026100100','digest':'a'*64}
        client.run=Mock(side_effect=['/mounted/forcing/gfs',json.dumps(entry)])
        result=client.snapshot_forcing_cycles('gfs',['/mounted/forcing/gfs/2026100100'])
        self.assertEqual(result[0]['path'],entry['path'])
        self.assertIn('/mounted/forcing/gfs',client.run.call_args.args[0])
        client.run=Mock(return_value='/mounted/forcing/gfs')
        with self.assertRaisesRegex(ValueError,'非受管'):
            client.snapshot_forcing_cycles('gfs',['/other/2026100100'])
        self.assertEqual(client.run.call_count,1)
        client.run=Mock(side_effect=['/mounted/forcing/gfs',json.dumps({**entry,'path':'/other/2026100100'})])
        with self.assertRaises(HpcError):
            client.snapshot_forcing_cycles('gfs',[entry['path']])

    def setUp(self):
        directory=TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root=Path(directory.name)
        self.store=TaskStore(self.root/'wrf.sqlite3')
        self.addCleanup(self.store.close)
        self.manager=SimpleNamespace(store=self.store,
            settings=SimpleNamespace(database_path=self.store.path,hpc_gfs_prefetch_enabled=False),
            gfs=SimpleNamespace(normalize_source=lambda x:x,latest_cycles=Mock(return_value=['2026100900'])),
            active_forcing_cycles=Mock(return_value=set()),retained_gfs_cycles=Mock(return_value=set()),
            hpc=Mock(),cleanup_forcing=Mock(return_value={'deleted':['/ordinary/2026100100']}))
        self.candidate={'remote_path':'/ordinary/2026100100','cleanup_allowed':True}
        self.manager.hpc.forcing_pool_items.return_value=[{'cycles':[self.candidate]}]
        self.manager.hpc.snapshot_forcing_cycles.return_value=[{'path':self.candidate['remote_path'],'digest':'a'*64}]
        self.service=ForcingCleanupService(self.manager)

    def test_automatic_busy_retry_reuses_id_and_unknown_is_not_replayed(self, actor="system"):
        import directory_delete as directories
        from hpc_transport import HpcCleanupBlockedError
        path=(self.root/'2026100100').resolve();path.mkdir()
        self.addCleanup(self.service.close)
        def wait_terminal(identifier):
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                current=self.store.forcing_cleanup_plan(identifier)
                if current['status'] in {'blocked','unknown','complete'}:return current
                time.sleep(.01)
            self.fail('cleanup did not reach a terminal state')
        candidate={'remote_path':str(path)}
        with patch.object(self.service,'_cycle_candidate',return_value=candidate), patch(
                'forcing_cleanup.directories.remote',return_value=[directories.identity(path)]), patch(
                'forcing_cleanup.directories.forcing_quarantine',side_effect=[HpcCleanupBlockedError('busy'),RuntimeError('lost response')]) as move:
            first=self.service.delete_cycle_async('gfs','2026100100',actor)
            self.assertTrue(wait_terminal(first['id'])['retryable_before_write'])
            second=self.service.delete_cycle_async('gfs','2026100100',actor)
            self.assertEqual(second['id'],first['id'])
            self.assertEqual(wait_terminal(first['id'])['status'],'unknown')
            third=self.service.delete_cycle_async('gfs','2026100100',actor)
            self.assertEqual(third['id'],first['id'])
            self.assertEqual(move.call_count,2)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM wrf_forcing_cleanup_plans').fetchone()[0],1)

    def test_manual_busy_retry_keeps_original_actor_and_operation(self):
        self.test_automatic_busy_retry_reuses_id_and_unknown_is_not_replayed(actor='alice')

    def test_refresh_accepts_without_waiting_and_preserves_holds(self):
        self.manager._forcing_cleanup_wakeup=threading.Event()
        release=threading.Event()
        self.addCleanup(self.service.close)
        self.addCleanup(release.set)
        def enable(*args,**kwargs):
            release.wait(2)
            self.assertEqual(kwargs['held_cycles'],{'gfs':[],'ecmwf':[]})
        with patch('forcing_policy.policy_update',side_effect=enable) as update:
            started=time.monotonic();operation=self.service.refresh_async('admin',3)
            self.assertLess(time.monotonic()-started,.3)
            self.assertEqual(self.service.refresh_async('admin',3)['id'],operation['id'])
            release.set();self.service.close()
            update.assert_called_once()
        self.assertTrue(self.manager._forcing_cleanup_wakeup.is_set())
        self.assertTrue(all(self.store.retention_approved(source) for source in ('gfs','ecmwf')))
        self.assertEqual(self.service.refresh_status()['status'],'scheduled')
        self.manager.cleanup_forcing.assert_not_called()

    def test_refresh_non_admin_cannot_enable_shared_cleanup_policy(self):
        self.manager._forcing_cleanup_wakeup=threading.Event()
        self.addCleanup(self.service.close)
        with patch('forcing_policy.policy_update') as update:
            self.service.refresh_async('ordinary',2);self.service.close()
            update.assert_not_called()
        self.assertEqual(self.service.refresh_status()['status'],'blocked')
        self.assertFalse(self.manager._forcing_cleanup_wakeup.is_set())
        self.assertFalse(self.store.retention_approved('gfs'))

    def test_refresh_policy_failure_does_not_enable_retention(self):
        self.manager._forcing_cleanup_wakeup=threading.Event()
        self.addCleanup(self.service.close)
        with patch('forcing_policy.policy_update',side_effect=ValueError('old cron still enabled')):
            self.service.refresh_async('admin',3);self.service.close()
        self.assertEqual(self.service.refresh_status()['status'],'blocked')
        self.assertFalse(self.manager._forcing_cleanup_wakeup.is_set())
        self.assertFalse(self.store.retention_approved('gfs'))

    def test_owner_expiry_digest_and_one_execution(self):
        plan=self.service.plan('gfs','admin-a',directory_mode=False)
        for actor,digest in [('admin-b',plan['digest']),('admin-a','b'*64)]:
            with self.assertRaises(ValueError):self.service.execute(plan['id'],actor,digest)
        with patch('forcing_cleanup.time.time',return_value=plan['expires_at']+1):
            with self.assertRaises(ValueError):self.service.execute(plan['id'],'admin-a',plan['digest'])
        self.manager.cleanup_forcing.assert_not_called()
        self.assertEqual(self.service.execute(plan['id'],'admin-a',plan['digest'])['status'],'complete')
        with self.assertRaises(ValueError):self.service.execute(plan['id'],'admin-a',plan['digest'])
        self.manager.cleanup_forcing.assert_called_once()
        self.assertEqual(self.manager.cleanup_forcing.call_args.kwargs['actor_sub'],'admin-a')

    def test_new_reference_blocks_execution_and_uncertain_reply_never_replays(self):
        plan=self.service.plan('gfs','admin',directory_mode=False)
        self.manager.hpc.forcing_pool_items.return_value=[{'cycles':[]}]
        with self.assertRaisesRegex(ValueError,'引用'):self.service.execute(plan['id'],'admin',plan['digest'])
        self.manager.cleanup_forcing.assert_not_called()
        self.manager.hpc.forcing_pool_items.return_value=[{'cycles':[self.candidate]}]
        self.manager.cleanup_forcing.side_effect=RuntimeError('lost reply')
        with self.assertRaisesRegex(RuntimeError,'lost reply'):self.service.execute(plan['id'],'admin',plan['digest'])
        self.assertEqual(self.service.status(plan['id'],'admin')['status'],'unknown')
        with self.assertRaises(ValueError):self.service.execute(plan['id'],'admin',plan['digest'])
        self.manager.cleanup_forcing.assert_called_once()

    def test_exact_bytes_changed_or_symlink_blocks_remote_cleanup(self):
        queue=PoolQueue(queue_args(self.root/'pool'))
        cycle='2026100100'
        file=queue.file_path(cycle,0)
        file.parent.mkdir(parents=True)
        file.write_bytes(b'original')
        digest=queue.snapshot(cycle)['digest']
        file.write_bytes(b'changed!')
        with patch('scripts.forcing_pool_queue.LOCK_ROOT',self.root/'locks'):
            self.assertEqual(queue.cleanup(cycle,digest),'BLOCKED')
            self.assertTrue(file.exists())
            fresh=queue.snapshot(cycle)['digest']
            self.assertEqual(queue.cleanup(cycle,fresh),'DELETED')
        file.parent.mkdir()
        file.symlink_to(self.root/'protected')
        with self.assertRaisesRegex(ValueError,'symbolic link'):queue.snapshot(cycle)

    def test_pending_downloads_protect_cycles_until_cancelled(self):
        from forcing_policy import request_download
        client=SimpleNamespace(trigger_forcing_download=Mock(side_effect=RuntimeError('unknown')))
        operation=request_download(self.store,client,'alice','gfs','2026100100',0,1)
        self.assertEqual(self.store.forcing_download_cycles('gfs'),{'2026100100'})
        self.store.update_forcing_operation(operation['id'],'cancelled',{})
        self.assertEqual(self.store.forcing_download_cycles('gfs'),set())

    def test_two_request_references_cancel_independently_and_preserve_prefetch(self):
        queue=PoolQueue(queue_args(self.root/'pool'))
        first,second='forcing_'+'a'*32,'forcing_'+'b'*32
        queue.enqueue('2026100100',[0,1],'task',first)
        queue.enqueue('2026100100',[1,2],'task',second)
        queue.enqueue('2026100100',[2],'prefetch')
        queue.release_reference('2026100100',first)
        entries=queue.load_state()['cycles']['2026100100']['files']
        self.assertEqual(entries['000']['status'],'cancelled')
        self.assertEqual(entries['001']['priority'],'task')
        self.assertEqual(set(entries['001']['references']),{second})
        queue.release_reference('2026100100',second)
        entries=queue.load_state()['cycles']['2026100100']['files']
        self.assertEqual(entries['001']['status'],'cancelled')
        self.assertEqual(entries['002']['status'],'queued')
        self.assertEqual(entries['002']['priority'],'prefetch')

    def test_idempotent_unknown_download_and_restart_preserve_references(self):
        from forcing_policy import request_download,cancel_download
        client=SimpleNamespace(trigger_forcing_download=Mock(side_effect=RuntimeError('lost reply')),
            release_forcing_reference=Mock(side_effect=RuntimeError('lost cancel reply')))
        op=request_download(self.store,client,'alice','gfs','2026100100',0,1,key='same')
        repeated=request_download(self.store,client,'alice','gfs','2026100100',0,1,key='same')
        self.assertEqual(op['id'],repeated['id'])
        client.trigger_forcing_download.assert_called_once()
        with self.assertRaises(ValueError):request_download(self.store,client,'alice','gfs','2026100100',0,2,key='same')
        cancelled=cancel_download(self.store,client,op)
        self.assertEqual(cancelled['status'],'cancel_unknown')
        self.assertEqual(self.store.forcing_download_cycles('gfs'),{'2026100100'})
        client.release_forcing_reference.side_effect=None
        self.assertEqual(cancel_download(self.store,client,cancelled)['status'],'cancelled')
        self.assertEqual(self.store.forcing_download_cycles('gfs'),set())
        self.store.update_forcing_operation(op['id'],'submitting',{})
        self.store.recover_interrupted_operations()
        self.assertEqual(self.store.forcing_operation(op['id'])['status'],'unknown')
        self.assertEqual(self.store.forcing_operation(op['id'])['detail']['hours'],[0,1])


if __name__=='__main__':unittest.main()
