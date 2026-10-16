"""Local queued tasks reserve budgets before worker dispatch."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock,patch
from config import settings
from database import TaskStore
from hpc import HpcClient
from task_manager import WrfTaskManager
from test_ordinary_ui2 import tree_request


class ResourceDispatchTest(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=replace(settings,database_path=Path(self.temp.name)/'db',ordinary_max_concurrent=4,hpc_runtime_release='new')
        self.store=TaskStore(self.config.database_path);self.addCleanup(self.store.close)
        self.manager=WrfTaskManager(self.config,self.store,None,HpcClient(self.config));self.manager._started=True
        self.manager._health={'status':'ready','cpu_count':20,'memory_available_mb':65536}
        self.thread=patch('task_manager.threading.Thread');self.threads=self.thread.start();self.addCleanup(self.thread.stop)
        for n in range(5):self.store.create(str(n),tree_request(),'owner')

    def test_four_workers_fifth_waits_and_terminal_worker_frees_capacity(self):
        for n in range(4):self.assertTrue(self.manager._dispatch(str(n)))
        self.assertFalse(self.manager._dispatch('4'));self.assertIn('4',self.store.get('4')['runtime']['resource_wait_reason'])
        self.manager._task_threads.pop('0');self.store.update('0',status='succeeded')
        self.assertTrue(self.manager._dispatch('4'))

    def test_insufficient_memory_cpu_or_unknown_health_stays_queued(self):
        for field,value,reason in [('memory_available_mb',2048,'内存'),('cpu_count',2,'CPU'),('status','checking','观测')]:
            original=dict(self.manager._health);self.manager._health[field]=value
            self.assertFalse(self.manager._dispatch('0'));self.assertIn(reason,self.store.get('0')['runtime']['resource_wait_reason'])
            self.manager._health=original
        self.assertEqual(self.store.get('0')['status'],'queued');self.threads.return_value.start.assert_not_called()

    def test_old_pinned_attempt_serializes_with_new_tasks(self):
        self.store.update('0',runtime={'release_id':'old'})
        self.assertTrue(self.manager._dispatch('0'));self.assertFalse(self.manager._dispatch('1'))
        self.assertIn('原发布',self.store.get('1')['runtime']['resource_wait_reason'])

    def test_unresolved_remote_attempt_keeps_its_reservation_after_restart(self):
        for n in range(4):self.store.update(str(n),status='paused_external',runtime={'remote_launch_attempted':True})
        self.assertFalse(self.manager._dispatch('4'))

    def test_previous_immutable_pool_supervisor_can_share_same_resource_protocol(self):
        self.store.update('0',runtime={'release_id':'old-compute','supervisor_release_id':'previous-pool','supervisor_fingerprint':'fixed'})
        self.assertTrue(self.manager._dispatch('0'))
        self.assertTrue(self.manager._dispatch('1'))
        self.store.update('0',runtime={'release_id':'old-compute','supervisor_release_id':'previous-pool'})
        self.assertFalse(self.manager._dispatch('2'))
