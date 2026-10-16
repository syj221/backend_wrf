"""Isolated regressions for responsive deletion and terminal forcing references."""
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from database import TaskStore
from retirement import sha
from task_deletion import TaskDeletionService
from task_manager import WrfTaskManager
from scripts.forcing_pool_queue import PoolQueue
from test_pool_resilience import queue_args

class SimplificationTest(TestCase):
    def setUp(self):
        self.store=TaskStore(Path(':memory:'))
        self.addCleanup(self.store.close)
        self.store.create('task',{'data_source':'gfs'},'alice')
        self.store.update('task',status='failed',stage='failed',runtime={'forcing_cycle':'2026090100'})

    def test_async_preview_returns_before_remote_and_deduplicates(self):
        release=threading.Event()
        task=self.store.get('task')
        revision=sha([task[k] for k in ('status','stage','attempt_no','request','runtime')]+[task.get('result')])
        def inspect(*args,**kwargs):
            args[-1]('inventory_assets')
            release.wait(2)
            return {'task_fingerprint':revision,'digest':'a'*64,'summary':{'files':0,'bytes':0,'published_assets':0,'protected':[]},'local':[],'remote':[]}
        service=TaskDeletionService(SimpleNamespace(),self.store,Mock())
        self.addCleanup(service.close)
        self.addCleanup(release.set)
        with patch('task_deletion.inventory',side_effect=inspect):
            started=time.monotonic();plan=service.plan_async('task','alice')
            self.assertLess(time.monotonic()-started,.3)
            self.assertEqual(service.plan_async('task','alice')['id'],plan['id'])
            with self.assertRaises(ValueError):service.status('task',plan['id'],'bob')
            self.assertIsNone(self.store.deletion(plan['id']).get('manifest'))
            release.set();service.close()
            self.assertEqual(service.status('task',plan['id'],'alice')['status'],'pending')
            self.assertIsNotNone(self.store.get('task'))

    def test_direct_delete_returns_before_inventory_and_executes_once(self):
        release=threading.Event()
        task=self.store.get('task')
        revision=sha([task[k] for k in ('status','stage','attempt_no','request','runtime')]+[task.get('result')])
        snapshot={'task_fingerprint':revision,'digest':'a'*64,'summary':{},'local':[],'remote':[]}
        def inspect(*args,**kwargs):
            release.wait(2)
            return snapshot
        service=TaskDeletionService(SimpleNamespace(),self.store,Mock())
        self.addCleanup(service.close)
        self.addCleanup(release.set)
        with patch('task_deletion.inventory',side_effect=inspect),patch.object(service,'execute') as execute:
            started=time.monotonic()
            plan=service.plan_async('task','alice',delete_now=True,bearer='fixture')
            self.assertLess(time.monotonic()-started,.3)
            self.assertTrue(plan['delete_now'])
            self.assertEqual(service.plan_async('task','alice',delete_now=True,bearer='fixture')['id'],plan['id'])
            execute.assert_not_called()
            release.set();service.close()
            execute.assert_called_once_with('task',plan['id'],'alice','a'*64,'fixture',_reserved=True)
            self.assertNotIn('fixture',str(self.store.deletion(plan['id'])))

    def test_direct_delete_failure_preserves_task_and_does_not_execute(self):
        service=TaskDeletionService(SimpleNamespace(),self.store,Mock())
        self.addCleanup(service.close)
        with patch('task_deletion.inventory',side_effect=RuntimeError('unknown identity')),patch.object(service,'execute') as execute:
            plan=service.plan_async('task','alice',delete_now=True,bearer='fixture')
            service.close()
            self.assertEqual(service.status('task',plan['id'],'alice')['status'],'blocked')
            self.assertIsNotNone(self.store.get('task'))
            execute.assert_not_called()

    def test_direct_delete_rejects_running_and_missing_credentials(self):
        service=TaskDeletionService(SimpleNamespace(),self.store,Mock())
        self.addCleanup(service.close)
        with self.assertRaises(ValueError):service.plan_async('task','alice',delete_now=True)
        self.store.update('task',status='running')
        with self.assertRaises(ValueError):service.plan_async('task','alice',delete_now=True,bearer='fixture')

    def test_reserved_execution_is_single_claim_and_never_replayed_after_restart(self):
        self.store.save_deletion({'id':'plan','task_id':'task','actor_sub':'alice','created_at':time.time(),'expires_at':time.time()+600,'status':'pending','phase':'preview'})
        self.assertTrue(self.store.reserve_deletion_execution('plan'))
        self.assertFalse(self.store.reserve_deletion_execution('plan'))
        self.store.recover_interrupted_operations()
        self.assertEqual(self.store.deletion('plan')['status'],'blocked')
        self.assertFalse(self.store.reserve_deletion_execution('plan'))

    def test_legacy_archive_filter_remains_but_new_list_includes_all(self):
        self.store.patch_metadata('task',0,{'archived':True})
        self.assertEqual(self.store.page(20,'alice')['items'],[])
        self.assertEqual([t['id'] for t in self.store.page(20,'alice',archived=None)['items']],['task'])
        self.assertEqual(self.store.page(20,'bob',archived=None)['items'],[])

    def test_terminal_release_is_explicit_fresh_and_state_bound(self):
        manager=object.__new__(WrfTaskManager);manager.store=self.store
        manager._forcing_terminal_proofs={'task':(sha(self.store.get('task')),time.monotonic())}
        self.assertIn('2026090100',manager.task_forcing_reference_counts('gfs'))
        self.assertEqual(manager.task_forcing_reference_counts('gfs',release_terminal=True),{})
        self.store.approve_retention('gfs','approved-exact-manifest')
        self.assertEqual(manager.task_forcing_reference_counts('gfs'),{})
        self.store.update('task',runtime={'forcing_cycle':'2026090100','changed':True})
        self.assertIn('2026090100',manager.task_forcing_reference_counts('gfs'))

    def test_cancelled_unknown_identity_keeps_forcing(self):
        self.store.update('task',status='cancelled')
        manager=object.__new__(WrfTaskManager);manager.store=self.store
        self.assertIn('2026090100',manager.task_forcing_reference_counts('gfs',release_terminal=True))

    def test_expired_prefetch_preserves_shared_consumers_and_inflight(self):
        with TemporaryDirectory() as directory:
            queue=PoolQueue(queue_args(Path(directory)/'pool'))
            queue.enqueue('2026090100',[0,1,2],'prefetch')
            queue.enqueue('2026090100',[1],'task','forcing_'+'a'*32)
            state=queue.load_state();state['cycles']['2026090100']['files']['002']['status']='downloading';queue.save_state(state)
            queue.expire_prefetch(['2026100900'])
            files=queue.load_state()['cycles']['2026090100']['files']
            self.assertEqual(files['000']['status'],'cancelled')
            self.assertNotEqual(files['001']['status'],'cancelled')
            self.assertEqual(files['002']['status'],'downloading')

    def test_expired_publication_is_not_an_active_download(self):
        from hpc_transport import HpcClient
        details=HpcClient._queue_cycle_details({'cycles':{'2026090100':{'publication':'expired','files':{'000':{'status':'cancelled'}}}}},'2026090100')
        self.assertEqual(details['queue_status'],'cancelled')

    def test_completed_recompute_requires_explicit_manual_request(self):
        from recovery import RecoveryService
        from test_guidance_recovery import request
        from test_recovery_diagnosis import runtime
        self.store.update('task',request=request(),runtime={'remote_wrf_succeeded':True})
        manager=SimpleNamespace(store=self.store,get=self.store.get,require_operation=Mock(),restart_plan=Mock(return_value={'can_restart':True,'local_paths':[],'remote_paths':[]}),restart=Mock())
        service=RecoveryService(manager,runtime)
        with self.assertRaisesRegex(ValueError,'计算已成功'):
            service.plan('task',None,'restart',actor_sub='alice',purpose='manual')
        plan=service.plan('task',None,'restart',request(),actor_sub='alice',purpose='manual')
        self.assertTrue(plan['recompute_completed'])
        service.execute('task',plan['id'],'alice')
        self.assertTrue(manager.restart.call_args.kwargs['allow_completed'])

    def test_completed_attempt_archives_raw_and_products_before_next_attempt(self):
        from config import settings
        from dataclasses import replace
        from test_namelist_config import request
        with TemporaryDirectory() as directory:
            root=Path(directory);identifier='wrf_gfs_20261008T000000Z_aaaaaaaa'
            config=replace(settings,database_path=root/'db',run_dir=root/'runs',data_dir=root/'out',runtime_asset_root=None,legacy_asset_manifest=None)
            store=TaskStore(config.database_path)
            try:
                store.create(identifier,request(),'alice')
                store.update(identifier,status='failed',stage='failed',runtime={'remote_wrf_succeeded':True,'release_id':'fixture'})
                run=config.run_dir/identifier;raw=run/'raw';raw.mkdir(parents=True);(raw/'frame.nc').write_bytes(b'isolated output')
                product=config.output_dir/'runs'/identifier;product.mkdir(parents=True);(product/'meta.json').write_text('{}')
                hpc=Mock();manager=WrfTaskManager(config,store,None,hpc);manager._dispatch=Mock();manager._log=Mock()
                with patch('terminal_evidence.verify_forcing_terminal',return_value={'terminal':True}):
                    result=manager.restart(identifier,request(),confirm_task_id=identifier,confirm_attempt=1,allow_completed=True)
                self.assertEqual(result['attempt_no'],2)
                self.assertEqual((run/'attempts/001/raw/frame.nc').read_bytes(),b'isolated output')
                self.assertTrue((run/'attempts/001/products/meta.json').is_file())
                self.assertEqual(store.attempts(identifier)[0]['attempt_no'],1)
                hpc.cleanup_task_attempt.assert_not_called()
            finally:store.close()

    def test_expanded_cleanup_requires_exact_confirmation_and_two_complete_cycles(self):
        from forcing_cleanup import ForcingCleanupService
        from config import settings
        from dataclasses import replace
        manager=SimpleNamespace(store=self.store,settings=replace(settings,database_path=self.store.path),
            gfs=SimpleNamespace(normalize_source=lambda source:source,latest_cycles=Mock(return_value=['2026100900','2026100800'])),
            refresh_forcing_terminal_proofs=Mock(),task_forcing_reference_counts=Mock(return_value={}),
            active_forcing_cycles=Mock(return_value=set()),retained_gfs_cycles=Mock(return_value=set()),
            hpc=Mock(),cleanup_forcing=Mock())
        candidate={'remote_path':'/ordinary/2026090100','cleanup_allowed':True,'complete':False}
        manager.hpc.forcing_pool_items.return_value=[{'targets_complete':False,'retained_complete_cycles':['2026100700','2026100600'],'cycles':[candidate]}]
        manager.hpc.snapshot_forcing_cycles.return_value=[{'path':candidate['remote_path'],'files':1,'bytes':10,'digest':'a'*64}]
        service=ForcingCleanupService(manager)
        self.assertFalse(self.store.retention_approved('gfs'))
        plan=service.plan('gfs','admin',retention_upgrade=True,directory_mode=False)
        self.assertEqual(plan['candidates'][0]['size_bytes'],10)
        self.assertFalse(self.store.retention_approved('gfs'))
        with self.assertRaises(ValueError):service.execute(plan['id'],'other-admin',plan['digest'])
        service.execute(plan['id'],'admin',plan['digest'])
        self.assertTrue(self.store.retention_approved('gfs'))
        manager.cleanup_forcing.assert_called_once()
        self.assertTrue(manager.cleanup_forcing.call_args.kwargs['retention_upgrade'])
        manager.hpc.forcing_pool_items.return_value=[{'retained_complete_cycles':['2026100700'],'cycles':[candidate]}]
        with self.assertRaisesRegex(ValueError,'不足两个'):
            service.plan('ecmwf','admin',retention_upgrade=True,directory_mode=False)

    def test_missing_latest_targets_do_not_block_approved_obsolete_partial_cleanup(self):
        from config import settings
        from dataclasses import replace
        manager=WrfTaskManager(replace(settings,database_path=self.store.path),self.store,Mock(),Mock())
        manager._health={'status':'ready'};manager._check_health=Mock()
        manager.gfs.latest_cycles.return_value=['2026100900','2026100800']
        manager.sync_latest_gfs=Mock(return_value={'actions':[]})
        manager.sync_latest_ecmwf=Mock(return_value={'actions':[]})
        manager.refresh_forcing_terminal_proofs=Mock()
        manager.cleanup_forcing=Mock()
        manager.hpc.forcing_pool_items.return_value=[{'targets_complete':False,'retained_complete_cycles':['2026100700','2026100600'],'cleanup_candidates':['/ordinary/2026090100']}]
        self.store.save_forcing_policy(0,True,True,{'gfs':[],'ecmwf':[]})
        manager._prefetch_latest_gfs_once()
        manager.cleanup_forcing.assert_not_called()
        self.store.approve_retention('gfs','exact-plan')
        manager._prefetch_latest_gfs_once()
        manager.cleanup_forcing.assert_called_once_with('gfs',['/ordinary/2026090100'])

    def test_unknown_delete_reopens_original_transaction_without_new_inventory(self):
        self.store.save_deletion({'id':'uncertain','task_id':'task','actor_sub':'alice','created_at':time.time()-700,'expires_at':time.time()-100,'status':'unknown','phase':'remote_deleted'})
        with self.store._db:
            self.store._db.execute("INSERT INTO wrf_task_tombstones VALUES (?,?,?,NULL)",('task','uncertain','deleting'))
        service=TaskDeletionService(SimpleNamespace(),self.store,Mock());self.addCleanup(service.close)
        with patch('task_deletion.inventory') as inventory:
            self.assertEqual(service.plan_async('task','alice')['id'],'uncertain')
            inventory.assert_not_called()

    def test_missing_persisted_managed_identity_blocks_before_ssh_or_inventory(self):
        self.store.update('task',runtime={'launch_attempt':'a'*32,'release_id':'fixture','remote_launch_attempted':True})
        service=TaskDeletionService(SimpleNamespace(),self.store,Mock());self.addCleanup(service.close)
        with patch('task_deletion.inventory') as inventory:
            plan=service.plan_async('task','alice');service.close()
            state=service.status('task',plan['id'],'alice')
            self.assertEqual(state['status'],'blocked')
            self.assertIn('进程身份',state['error']['message'])
            inventory.assert_not_called()
