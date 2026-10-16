"""Isolated fixed-directory quarantine and no-content-read regressions."""
import json
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch, Mock

import directory_delete as deletion
from deletion_queue import DeletionQueue
from database import TaskStore
from hpc_transport import HpcCleanupBlockedError


class LocalClient:
    def __init__(self, root=None):
        self.root=root
    def run(self, command, timeout=30):
        return subprocess.check_output(command,shell=True,text=True,timeout=timeout)
    def _forcing_settings(self,source):
        return {'root':str(self.root),'script':str(Path(__file__).parent/'scripts/download_gfs_00z.sh')}


class DirectoryDeletionTest(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()

    def test_moves_whole_directory_without_reading_content_and_preserves_symlink_target(self):
        path=self.root/'task';path.mkdir();(path/'huge').write_bytes(b'payload')
        shared=self.root/'shared';shared.write_text('protected');(path/'link').symlink_to(shared)
        snapshot=deletion.identity(path);item=deletion.intent(snapshot,'delete_test')
        with patch('pathlib.Path.open',side_effect=AssertionError('Must not read contents')):
            deletion.quarantine(item)
            self.assertFalse(path.exists());self.assertTrue(Path(item['trash'],'task').exists())
            # A fresh task at the old path must never be deleted by deferred cleanup.
            path.mkdir();__import__('os').write(__import__('os').open(str(path/'new'),__import__('os').O_WRONLY|__import__('os').O_CREAT,0o600),b'new')
            deletion.purge(item)
        self.assertEqual(shared.read_text(),'protected');self.assertEqual((path/'new').read_bytes(),b'new')

    def test_replacement_link_and_unsafe_trash_are_rejected(self):
        path=self.root/'task';path.mkdir();snapshot=deletion.identity(path);item=deletion.intent(snapshot,'delete_test')
        path.rmdir();path.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):deletion.quarantine(item)
        path.unlink();path.mkdir();item=deletion.intent(deletion.identity(path),'delete_test')
        (self.root/'.wrf-trash').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(OSError):deletion.quarantine(item)
        self.assertTrue(path.exists())

    def test_root_identity_change_prevents_move_and_purge(self):
        path=self.root/'task';path.mkdir();item=deletion.intent(deletion.identity(path),'delete_test')
        path.rename(self.root/'retained');path.mkdir()
        with self.assertRaises(ValueError):deletion.quarantine(item)
        item=deletion.intent(deletion.identity(path),'delete_test');deletion.quarantine(item)
        trash=Path(item['trash'])/'task';trash.rename(self.root/'retained2');trash.mkdir()
        with self.assertRaises(ValueError):deletion.purge(item)

    def test_remote_bridge_moves_multiple_roots_and_never_replays(self):
        paths=[self.root/name for name in ('task','artifact')];paths[0].mkdir();paths[1].write_text('log')
        client=LocalClient();snapshots=deletion.remote(client,'identity',list(map(str,paths)))
        moves=[deletion.intent(s,'delete_remote') for s in snapshots]
        deletion.remote(client,'quarantine',moves)
        with self.assertRaises(subprocess.CalledProcessError):deletion.remote(client,'quarantine',moves)
        deletion.remote(client,'purge',moves)
        self.assertFalse(Path(moves[0]['trash']).exists())

    def test_absent_parent_is_a_noop_not_a_recursive_creation(self):
        path=self.root/'missing/parent/task';item=deletion.intent(deletion.identity(path),'delete_test')
        deletion.quarantine(item);deletion.purge(item);self.assertFalse(path.parent.exists())

    def test_forcing_bridge_uses_installed_queue_locks_and_preserves_metadata_audit(self):
        from scripts.forcing_pool_queue import PoolQueue
        root=self.root/'forcing';root.mkdir();client=LocalClient(root)
        queue=PoolQueue(SimpleNamespace(root=str(root),source='gfs'))
        cycle='2026100100';path=root/cycle;path.mkdir();(path/'data').write_text('payload')
        queue.save_state({'version':1,'source':'gfs','cycles':{cycle:{'files':{'f':{'priority':'task','status':'pending'}}}}})
        item=deletion.intent(deletion.identity(path),'cycle_delete_test')
        with self.assertRaises(HpcCleanupBlockedError):deletion.forcing_quarantine(client,'gfs',item)
        self.assertTrue(path.exists())
        queue.save_state({'version':1,'source':'gfs','cycles':{cycle:{'files':{}}}})
        deletion.forcing_quarantine(client,'gfs',item)
        self.assertFalse(path.exists());self.assertNotIn(cycle,queue.load_state()['cycles'])
        self.assertEqual(json.loads((root/'logs/quarantine_cycle_delete_test_2026100100.json').read_text())['status'],'removed')
        deletion.remote(client,'purge',[item])

    def test_forcing_cycle_lock_blocks_same_cycle_not_other_cycle(self):
        from scripts.forcing_pool_queue import PoolQueue, exclusive
        root=self.root/'cycle-pool';root.mkdir();client=LocalClient(root)
        queue=PoolQueue(SimpleNamespace(root=str(root),source='gfs'))
        cycle='2026100100';path=root/cycle;path.mkdir();(path/'data').write_text('payload')
        item=deletion.intent(deletion.identity(path),'cycle_delete_isolation')
        with exclusive(queue.cycle_lock(cycle)):
            with self.assertRaises(HpcCleanupBlockedError):deletion.forcing_quarantine(client,'gfs',item)
        self.assertTrue(path.exists())
        with exclusive(queue.cycle_lock('2026100200')):
            deletion.forcing_quarantine(client,'gfs',item)
        deletion.remote(client,'purge',[item])

    def test_busy_worker_gate_without_live_owner_blocks_legacy_orphan(self):
        from scripts.forcing_pool_queue import PoolQueue, kernel_guard
        root=self.root/'orphan-pool';root.mkdir();client=LocalClient(root)
        queue=PoolQueue(SimpleNamespace(root=str(root),source='gfs'))
        path=root/'2026100100';path.mkdir()
        with kernel_guard(queue.worker_lock):
            with self.assertRaises(HpcCleanupBlockedError):
                deletion.forcing_quarantine(client,'gfs',deletion.intent(deletion.identity(path),'cycle_delete_orphan'))
        self.assertTrue(path.exists())

    def test_ready_file_with_task_reference_still_blocks_cycle_removal(self):
        from scripts.forcing_pool_queue import PoolQueue
        root=self.root/'reference-pool';root.mkdir();client=LocalClient(root)
        queue=PoolQueue(SimpleNamespace(root=str(root),source='gfs'))
        path=root/'2026100100';path.mkdir()
        queue.save_state({'version':1,'source':'gfs','cycles':{'2026100100':{'files':{
            '000':{'status':'ready','references':{'task:retained':True}}}}}})
        with self.assertRaises(HpcCleanupBlockedError):
            deletion.forcing_quarantine(client,'gfs',deletion.intent(deletion.identity(path),'cycle_delete_ref'))
        self.assertTrue(path.exists())

    def test_slow_space_cleanup_does_not_block_next_directory_removal(self):
        queue=DeletionQueue();release=threading.Event();started=threading.Event();next_removed=threading.Event()
        def slow():started.set();release.wait(3)
        try:
            queue.submit('one',lambda:queue.cleanup_submit(slow),lambda _:None)
            self.assertTrue(started.wait(1))
            queue.submit('two',next_removed.set,lambda _:None)
            self.assertTrue(next_removed.wait(1))
        finally:release.set();queue.close()

    def test_new_batch_forcing_plan_uses_root_identity_and_background_quarantine(self):
        from forcing_cleanup import ForcingCleanupService
        root=self.root/'forcing';root.mkdir();path=root/'2026100100';path.mkdir();(path/'data').write_text('synthetic')
        store=TaskStore(self.root/'tasks.db');self.addCleanup(store.close)
        manager=SimpleNamespace(store=store,settings=SimpleNamespace(database_path=store.path,hpc_gfs_prefetch_enabled=False),
            hpc=LocalClient(root),gfs=SimpleNamespace(normalize_source=lambda x:x,latest_cycles=lambda **kw:[]),
            active_forcing_cycles=lambda _:set(),retained_gfs_cycles=lambda:set())
        manager.hpc.forcing_pool_items=lambda *args:[{'cycles':[dict(cycle=path.name,remote_path=str(path),cleanup_allowed=True)]}]
        service=ForcingCleanupService(manager)
        try:
            plan=service.plan('gfs','alice')
            self.assertTrue(plan['directory_mode'])
            service.execute(plan['id'],'alice',plan['digest'])
            service.queue.worker.submit(lambda:None).result(3)
            service.queue.cleanup_worker.submit(lambda:None).result(3)
            self.assertEqual(service.status(plan['id'],'alice')['status'],'complete')
            self.assertTrue(service.status(plan['id'],'alice')['removed']);self.assertFalse(path.exists())
        finally:service.close()

    def test_task_transaction_removes_record_before_slow_cleanup_without_hashing(self):
        from task_deletion import TaskDeletionService
        root=self.root/'task';root.mkdir();(root/'data').write_text('owned')
        remote=self.root/'remote';remote.mkdir();(remote/'data').write_text('remote owned')
        store=TaskStore(self.root/'tasks.db');self.addCleanup(store.close)
        task='wrf_gfs_20261009T000000Z_01234567'
        store.create(task,{'data_source':'gfs'},'alice');store.update(task,status='succeeded',stage='done')
        service=TaskDeletionService(SimpleNamespace(),store,LocalClient())
        release=threading.Event();entered=threading.Event()
        original=service._purge_quarantine
        def slow(identifier,moves):
            entered.set();release.wait(3);original(identifier,moves)
        catalog=[dict(task_id=task,references=[],assets=[],record=None)]
        try:
            with patch('task_deletion._roots',return_value=([str(root)],[str(remote)],str(remote))),patch('task_deletion.remote_call',return_value=[{'verified':True,'status':'succeeded'}]),patch('task_deletion.init_database',return_value=(None,None)),patch('task_deletion.catalog_snapshot',return_value=catalog),patch('task_deletion.observe_static_access',return_value=[]),patch('task_deletion.scan_root',side_effect=AssertionError('No hashes')),patch.object(service,'_purge_quarantine',side_effect=slow):
                plan=service.plan_async(task,'alice',delete_now=True,bearer='isolated-fixture')
                self.assertTrue(entered.wait(2))
                service.queue.worker.submit(lambda:None).result(2)
                state=service.status(task,plan['id'],'alice')
                self.assertTrue(state['removed']);self.assertEqual(state['phase'],'space_cleanup')
                self.assertIsNone(store.get(task));self.assertFalse(root.exists());self.assertFalse(remote.exists())
                with self.assertRaises(ValueError):service.status(task,plan['id'],'bob')
                release.set();service.queue.cleanup_worker.submit(lambda:None).result(3)
                self.assertEqual(service.status(task,plan['id'],'alice')['status'],'complete')
        finally:release.set();service.close()

    def test_fuseblk_masked_permissions_require_kernel_checks_and_private_group(self):
        import os
        import grp
        import pwd
        path=self.root/'task';path.mkdir();item=deletion.intent(deletion.identity(path),'delete_permissions')
        trash=self.root/'.wrf-trash';trash.mkdir();trash.chmod(0o775)
        account=SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid())
        with patch('os.getxattr',create=True,return_value=b''),patch('directory_delete.mount_for_fd',return_value=('fuseblk',{'default_permissions'})),patch.object(grp,'getgrgid',return_value=SimpleNamespace(gr_mem=[])),patch.object(pwd,'getpwall',return_value=[account]):
            self.assertEqual(deletion.location(item),'unmoved')
            deletion.quarantine(item);self.assertEqual(deletion.location(item),'quarantined');deletion.purge(item)
        path.mkdir();item=deletion.intent(deletion.identity(path),'delete_permissions2')
        cases=[('ext4',{'default_permissions'},[account]),('fuseblk',set(),[account]),('fuseblk',{'default_permissions'},[account,SimpleNamespace(pw_uid=os.getuid()+1,pw_gid=os.getgid())])]
        for fs,options,members in cases:
            with self.subTest(fs=fs,options=options,members=len(members)),patch('os.getxattr',create=True,side_effect=OSError(__import__('errno').ENODATA,'no ACL')),patch('directory_delete.mount_for_fd',return_value=(fs,options)),patch.object(grp,'getgrgid',return_value=SimpleNamespace(gr_mem=[])),patch.object(pwd,'getpwall',return_value=members),self.assertRaisesRegex(ValueError,'Unsafe quarantine'):
                deletion.quarantine(item)
        with patch('os.getxattr',create=True,return_value=b'unknown ACL'),patch('directory_delete.mount_for_fd',return_value=('fuseblk',{'default_permissions'})),self.assertRaisesRegex(ValueError,'Unsafe quarantine'):
            deletion.quarantine(item)
        trash.chmod(0o777)
        with patch('directory_delete.private_fuseblk_group',return_value=True),self.assertRaisesRegex(ValueError,'Unsafe quarantine'):
            deletion.quarantine(item)
        self.assertTrue(path.exists())

    def test_unmoved_reconciliation_preserves_original_id_and_rejects_partial_or_changed(self):
        from task_deletion import TaskDeletionService,maintenance_unmoved_proof
        root=self.root/'task';root.mkdir();(root/'data').write_text('owned')
        remote=self.root/'remote';remote.mkdir();(remote/'data').write_text('remote owned')
        store=TaskStore(self.root/'tasks.db');self.addCleanup(store.close)
        task='wrf_gfs_20261009T000000Z_01234567'
        store.create(task,{'data_source':'gfs'},'alice');store.update(task,status='succeeded',stage='done')
        service=TaskDeletionService(SimpleNamespace(),store,LocalClient())
        catalog=[dict(task_id=task,references=[],assets=[],record=None)]
        original_remote=deletion.remote
        fail=[True]
        def transport(client,operation,payload,**kwargs):
            if operation=='quarantine' and fail[0]:raise ValueError('Unsafe quarantine ownership or permissions')
            return original_remote(client,operation,payload,**kwargs)
        try:
            with patch('task_deletion._roots',return_value=([str(root)],[str(remote)],str(remote))),patch('task_deletion.remote_call',return_value=[{'verified':True,'status':'succeeded'}]),patch('task_deletion.init_database',return_value=(None,None)),patch('task_deletion.catalog_snapshot',return_value=catalog),patch('task_deletion.observe_static_access',return_value=[]),patch('directory_delete.remote',side_effect=transport):
                plan=service.plan_async(task,'alice',delete_now=True,bearer='isolated-fixture')
                service.queue.worker.submit(lambda:None).result(3)
                before=store.deletion(plan['id']);self.assertEqual(before['status'],'unknown')
                tombstone=dict(task_id=task,deletion_plan_id=plan['id'],status='deleting')
                proof=maintenance_unmoved_proof(SimpleNamespace(),before,store.get(task),tombstone,LocalClient())
                self.assertTrue(all(x=='unmoved' for x in proof['locations']))
                with self.assertRaises(ValueError):service.reconcile(task,plan['id'],'bob','isolated-fixture')
                with patch('task_deletion.observe_static_access',return_value=[{'status':200}]),self.assertRaisesRegex(ValueError,'认证访问'):
                    service.reconcile(task,plan['id'],'alice','isolated-fixture')
                move=before['quarantine']['remote'][0];deletion.quarantine(move)
                with self.assertRaisesRegex(ValueError,'已移动'):
                    service.reconcile(task,plan['id'],'alice','isolated-fixture')
                with self.assertRaisesRegex(RuntimeError,'roots changed|moved or missing'):
                    maintenance_unmoved_proof(SimpleNamespace(),before,store.get(task),tombstone,LocalClient())
                # Restore only this isolated fixture to its original identity.
                (Path(move['trash'])/move['name']).rename(remote)
                fail[0]=False
                result=service.reconcile(task,plan['id'],'alice','isolated-fixture')
                self.assertEqual(result['id'],plan['id'])
                service.queue.worker.submit(lambda:None).result(3)
                service.queue.cleanup_worker.submit(lambda:None).result(3)
                final=store.deletion(plan['id'])
                self.assertEqual(final['status'],'complete');self.assertTrue(final['removed'])
                self.assertEqual(len(final['reconciliations']),1);self.assertIsNone(store.get(task))
                self.assertFalse(remote.exists());self.assertFalse(root.exists())
        finally:service.close()

    def test_accepted_reconciliation_restart_stays_unknown_with_original_tombstone(self):
        from retirement import sha
        from deletion_queue import timings
        store=TaskStore(self.root/'reconciliation.db');self.addCleanup(store.close)
        store.create('task',{'data_source':'gfs'},'alice');store.update('task',status='failed',stage='failed')
        task=store.get('task');fingerprint=sha([task[k] for k in ('status','stage','attempt_no','request','runtime')]+[task.get('result')])
        store.save_deletion(dict(id='delete_test',task_id='task',actor_sub='alice',status='pending',phase='preview',manifest={'task_fingerprint':fingerprint},created_at=1,started_at=2,expires_at=9999999999))
        self.assertTrue(store.claim_deletion('delete_test','task'));store.update_deletion_detail('delete_test',status='unknown',phase='quarantining')
        self.assertFalse(store.reserve_directory_reconciliation('delete_test','task','bob',{'verified_at':100}))
        self.assertTrue(store.reserve_directory_reconciliation('delete_test','task','alice',{'verified_at':100}))
        accepted=store.deletion('delete_test');self.assertEqual(accepted['status'],'executing');self.assertIsNone(accepted['started_at'])
        with patch('deletion_queue.time.time',return_value=110):
            self.assertEqual(timings(accepted)['wait_seconds'],10)
            self.assertEqual(timings(accepted)['execution_seconds'],0)
        self.assertFalse(store.reserve_directory_reconciliation('delete_test','task','alice',{'verified_at':101}))
        store.recover_interrupted_operations()
        self.assertEqual(store.deletion('delete_test')['status'],'unknown');self.assertTrue(store.deletion_blocked('task'))
        self.assertEqual(len(store.deletion('delete_test')['reconciliations']),1)

    def test_async_reconcile_and_forcing_execute_routes_import_their_response_class(self):
        import ast
        from contextlib import nullcontext
        from fastapi import Request,HTTPException
        from hpc import HpcError
        tree=ast.parse((Path(__file__).parent/'main.py').read_text())
        imports=[node for node in tree.body if isinstance(node,ast.ImportFrom) and node.module=='fastapi.responses']
        nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in {'task_deletion_reconcile','execute_forcing_cleanup_plan'}]
        for node in nodes:node.decorator_list=[]
        service=SimpleNamespace(reconcile=Mock(return_value={'status':'executing','id':'original'}),execute=Mock(return_value={'status':'checking','id':'cycle'}))
        namespace=dict(Request=Request,RecoveryConfirmation=object,ForcingCleanupConfirmation=object,HTTPException=HTTPException,HpcError=HpcError,
            _viewer=lambda _:('alice',3),task_manager=SimpleNamespace(_operation_lock=lambda _:nullcontext()),deletion_service=service,forcing_cleanup_service=service,ok=lambda data:{'data':data})
        # Import the response class from the actual module, not a test-only injection.
        exec(compile(ast.Module(body=imports+nodes,type_ignores=[]),'main.py','exec'),namespace)
        request=SimpleNamespace(headers={'authorization':'Bearer isolated-fixture'})
        response=namespace['task_deletion_reconcile']('task','original',SimpleNamespace(confirmed=True),request)
        self.assertEqual(response.status_code,202);self.assertEqual(json.loads(response.body)['data']['id'],'original')
        response=namespace['execute_forcing_cleanup_plan']('cycle',SimpleNamespace(digest='a'*64),request)
        self.assertEqual(response.status_code,202)

    def test_removed_task_survives_restart_as_cleanup_unknown_not_replayed(self):
        store=TaskStore(self.root/'tasks.db');self.addCleanup(store.close)
        store.create('task',{'data_source':'gfs'},'alice');store.update('task',status='failed',stage='failed')
        from retirement import sha
        task=store.get('task')
        fingerprint=sha([task[k] for k in ('status','stage','attempt_no','request','runtime')]+[task.get('result')])
        store.save_deletion(dict(manifest={'task_fingerprint':fingerprint},id='delete_test',task_id='task',actor_sub='alice',status='pending',phase='preview',created_at=1,expires_at=9999999999))
        self.assertTrue(store.claim_deletion('delete_test','task'))
        store.purge_deleted_task('task','delete_test',cleanup_pending=True)
        self.assertIsNone(store.get('task'));self.assertTrue(store.deletion('delete_test')['removed'])
        store.recover_interrupted_operations()
        self.assertEqual(store.deletion('delete_test')['status'],'unknown')
        self.assertTrue(store.deletion('delete_test')['removed'])

if __name__=='__main__':unittest.main()
