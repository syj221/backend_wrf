"""Compute-free FIFO, phase budgets, and no-replay regressions."""
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from database import TaskStore
from deletion_queue import timings
from task_deletion import TaskDeletionService, _DeadlineClient
from forcing_cleanup import ForcingCleanupService
from hpc import HpcError
from retirement import sha, scan_root
from terminal_evidence import terminal_mode


class DeletionQueueTest(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=TaskStore(Path(self.directory.name)/'tasks.sqlite3')
        self.addCleanup(self.store.close)
        self.remote=Mock(side_effect=lambda client,op,payload,**kwargs: [dict(path=x,exists=False,parent_device=1,parent_inode=2) for x in payload] if op=='identity' else [])
        self.transport_patch=patch('directory_delete.remote',self.remote);self.transport_patch.start();self.addCleanup(self.transport_patch.stop)
        self.quarantine=Mock()
        self.move_patch=patch('directory_delete.forcing_quarantine',self.quarantine);self.move_patch.start();self.addCleanup(self.move_patch.stop)
        self.service=TaskDeletionService(SimpleNamespace(),self.store,Mock())
        self.addCleanup(self.service.close)

    def task(self, name):
        self.store.create(name,{'data_source':'gfs'},'alice')
        self.store.update(name,status='failed',stage='failed')

    def snapshot(self, identity):
        task=self.store.get(identity)
        return dict(task_fingerprint=sha([task[k] for k in ('status','stage','attempt_no','request','runtime')]+[task.get('result')]),
                    digest='a'*64,summary={},local=[],remote=[])

    def test_eight_tasks_are_fifo_deduplicated_bounded_and_failure_does_not_block(self):
        release,started=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        order=[]
        def inventory(_settings,_store,_client,identity,*args,**kwargs):
            order.append(identity)
            if identity=='task0':started.set();release.wait(3)
            if identity=='task3':raise HpcError('connection lost')
            return self.snapshot(identity)
        for n in range(9):self.task('task'+str(n))
        with patch('task_deletion.inventory',side_effect=inventory):
            first=self.service.plan_async('task0','alice');self.assertTrue(started.wait(1))
            plans=[first]+[self.service.plan_async('task'+str(n),'alice') for n in range(1,8)]
            self.assertEqual(self.service.plan_async('task1','alice')['id'],plans[1]['id'])
            for n in range(1,8):
                state=self.service.status('task'+str(n),plans[n]['id'],'alice')
                self.assertEqual(state['phase'],'queued');self.assertEqual(state['queue_position'],n)
                self.assertIsNone(state['started_at']);self.assertEqual(state['execution_seconds'],0)
            with self.assertRaisesRegex(ValueError,'队列已满'):self.service.plan_async('task8','alice')
            release.set();self.service._worker.submit(lambda:None).result(3)
            self.assertEqual(order,['task'+str(n) for n in range(8)])
            self.assertEqual(self.store.deletion(plans[3]['id'])['status'],'blocked')
            self.assertEqual(self.store.deletion(plans[7]['id'])['status'],'pending')

    def manager(self):
        manager=SimpleNamespace(store=self.store,settings=SimpleNamespace(database_path=self.store.path,hpc_gfs_prefetch_enabled=False),
                                hpc=Mock(),cleanup_forcing=Mock())
        return manager

    def test_cycle_deletion_uses_task_fifo_and_cannot_run_in_parallel(self):
        release,started=threading.Event(),threading.Event();self.addCleanup(release.set)
        manager=self.manager();cycle=ForcingCleanupService(manager,self.service.queue);self.addCleanup(cycle.close)
        candidate={'cycle':'2026100100','remote_path':'/ordinary/2026100100','size_bytes':10,'cleanup_allowed':True}
        manager.hpc.snapshot_forcing_cycles.return_value=[{'path':candidate['remote_path'],'digest':'a'*64}]
        def inventory(*args,**kwargs):started.set();release.wait(3);return self.snapshot('task')
        self.task('task')
        with patch('task_deletion.inventory',side_effect=inventory),patch.object(cycle,'_cycle_candidate',return_value=candidate):
            self.service.plan_async('task','alice');self.assertTrue(started.wait(1))
            operation=cycle.delete_cycle_async('gfs','2026100100','alice')
            self.assertEqual(cycle.status(operation['id'],'alice')['queue_position'],1)
            manager.cleanup_forcing.assert_not_called()
            release.set();self.service._worker.submit(lambda:None).result(3)
            self.service.queue.cleanup_worker.submit(lambda:None).result(3)
            self.assertEqual(cycle.status(operation['id'],'alice')['status'],'complete')
            self.quarantine.assert_called_once()

    def test_cycle_protection_and_uncertain_write_never_replay(self):
        manager=self.manager();cycle=ForcingCleanupService(manager,self.service.queue);self.addCleanup(cycle.close)
        with patch.object(cycle,'_cycle_candidate',side_effect=ValueError('任务引用保护')):
            plan=cycle.delete_cycle_async('gfs','2026100100','alice');self.service._worker.submit(lambda:None).result(3)
            self.assertEqual(cycle.status(plan['id'],'alice')['status'],'blocked')
            manager.cleanup_forcing.assert_not_called()
        candidate={'remote_path':'/ordinary/2026100100','size_bytes':100}
        manager.hpc.snapshot_forcing_cycles.return_value=[{'path':candidate['remote_path']}]
        self.quarantine.side_effect=HpcError('response lost')
        with patch.object(cycle,'_cycle_candidate',return_value=candidate):
            plan=cycle.delete_cycle_async('gfs','2026100100','alice');self.service._worker.submit(lambda:None).result(3)
            self.assertEqual(cycle.status(plan['id'],'alice')['status'],'unknown')
            self.assertEqual(cycle.delete_cycle_async('gfs','2026100100','alice')['id'],plan['id'])
            with self.assertRaises(ValueError):cycle.delete_cycle_async('gfs','2026100100','bob')
            self.quarantine.assert_called_once()

    def test_confirmed_remote_busy_is_blocked_instead_of_unknown(self):
        from hpc_transport import HpcCleanupBlockedError
        manager=self.manager();cycle=ForcingCleanupService(manager,self.service.queue);self.addCleanup(cycle.close)
        candidate={'remote_path':'/ordinary/2026100100','size_bytes':100}
        manager.hpc.snapshot_forcing_cycles.return_value=[{'path':candidate['remote_path']}]
        self.quarantine.side_effect=HpcCleanupBlockedError('下载中，未清理')
        with patch.object(cycle,'_cycle_candidate',return_value=candidate):
            plan=cycle.delete_cycle_async('gfs','2026100100','alice');self.service._worker.submit(lambda:None).result(3)
            self.assertEqual(cycle.status(plan['id'],'alice')['status'],'blocked')
            self.assertIn('未删除',cycle.status(plan['id'],'alice')['error']['message'])
            self.quarantine.assert_called_once()

    def test_real_candidate_reference_protection_prevents_snapshot_and_cleanup(self):
        manager=self.manager();manager.gfs=SimpleNamespace(latest_cycles=Mock(return_value=['2026101300','2026101200']))
        manager.refresh_forcing_terminal_proofs=Mock();manager.task_forcing_reference_counts=Mock(return_value={'2026100100':1})
        manager.hpc.forcing_pool_items.return_value=[{'cycles':[{'cycle':'2026100100','cleanup_allowed':False}]}]
        cycle=ForcingCleanupService(manager,self.service.queue);self.addCleanup(cycle.close)
        plan=cycle.delete_cycle_async('gfs','2026100100','alice');self.service._worker.submit(lambda:None).result(3)
        self.assertEqual(cycle.status(plan['id'],'alice')['status'],'blocked')
        manager.hpc.snapshot_forcing_cycles.assert_not_called();manager.cleanup_forcing.assert_not_called()
        protected=manager.hpc.forcing_pool_items.call_args.args[2]
        self.assertIn('2026100100',protected)

    def test_cycle_route_requires_admin_before_acceptance(self):
        import ast
        from fastapi import HTTPException
        tree=ast.parse((Path(__file__).parent/'main.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='delete_forcing_cycle');node.decorator_list=[]
        service=Mock();viewer=Mock(return_value=('alice',2))
        namespace=dict(Request=object,HTTPException=HTTPException,_viewer=viewer,forcing_cleanup_service=service,ok=lambda data:data)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'main.py','exec'),namespace)
        with self.assertRaises(HTTPException) as caught:namespace['delete_forcing_cycle']('gfs','2026100100',object())
        self.assertEqual(caught.exception.status_code,403);service.delete_cycle_async.assert_not_called()
        viewer.return_value=('alice',3);namespace['delete_forcing_cycle']('gfs','2026100100',object())
        service.delete_cycle_async.assert_called_once_with('gfs','2026100100','alice')

    def test_restart_interrupts_queued_operations_without_replay(self):
        self.store.save_forcing_cleanup_plan(dict(id='cycle',actor_sub='alice',source='gfs',cycle='2026100100',status='checking',phase='queued',created_at=time.time()))
        self.store.recover_interrupted_operations()
        self.assertEqual(self.store.forcing_cleanup_plan('cycle')['status'],'blocked')
        self.assertEqual(self.store.forcing_cleanup_plan('cycle')['phase'],'interrupted')
        self.assertIsNotNone(self.store.forcing_cleanup_plan('cycle')['finished_at'])

    def test_wait_and_execution_clocks_are_separate(self):
        with patch('deletion_queue.time.time',return_value=120):
            state=timings({'created_at':10,'started_at':100})
            self.assertEqual(state['wait_seconds'],90);self.assertEqual(state['execution_seconds'],20)
            state=timings({'created_at':10})
            self.assertEqual(state['wait_seconds'],110);self.assertEqual(state['execution_seconds'],0)

    def test_budget_uses_metadata_and_allows_scan_beyond_thirty_seconds(self):
        raw=Mock();client=_DeadlineClient(raw)
        with patch('task_deletion.remote_call',return_value=[{'bytes':2*1024**3}]):
            client.remote('scan-command','scan',['/owned'],3600)
        self.assertGreater(raw.run.call_args.kwargs['timeout'],200)
        client.remote('paths-command','paths',['/owned'],3600)
        self.assertEqual(raw.run.call_args.kwargs['timeout'],30)
        client.deadline=time.monotonic()-1
        with self.assertRaises(TimeoutError):client.run('scan-command',timeout=900)

    def test_read_retry_is_bounded_and_dispatched_write_is_never_repeated(self):
        raw=Mock();raw.run.side_effect=[HpcError('tx-lab 命令执行超时'),HpcError('connection lost'),'[]']
        client=_DeadlineClient(raw)
        with patch('task_deletion.time.sleep') as sleep:
            client.remote('paths','paths',[],30)
            self.assertEqual([call.args[0] for call in sleep.call_args_list],[2,5])
        raw.reset_mock();raw.run.side_effect=HpcError('response lost')
        with patch('task_deletion.remote_call',return_value=[{'bytes':0}]),self.assertRaises(HpcError):
            client.remote('delete','delete',[{'path':'/owned'}],120)
        self.assertEqual(raw.run.call_count,1)
        raw.reset_mock();client.allow_retries=False
        with self.assertRaises(HpcError):client.remote('paths','paths',[],30)
        self.assertEqual(raw.run.call_count,1)

    def test_local_hash_deadline_refuses_before_deletion(self):
        root=Path(self.directory.name)/'owned';root.write_bytes(b'data')
        with self.assertRaises(TimeoutError):scan_root(root,deadline=time.monotonic()-1)
        self.assertTrue(root.exists())

    def test_missing_process_is_allowed_only_for_prelaunch_cancellation_probe(self):
        task={'status':'cancelled','runtime':{'launch_attempt':'a'*32,'remote_launch_attempted':True,'remote_terminal_status':'cancelled','remote_process':None}}
        self.assertEqual(terminal_mode(task),'managed')
        task['status']='running'
        with self.assertRaises(RuntimeError):terminal_mode(task)
