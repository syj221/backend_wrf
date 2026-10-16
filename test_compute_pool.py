"""Resource admission is durable, bounded and never replays unknown launches."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch, Mock
import json,os
from scripts import managed_process as p


class ComputePoolTest(TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'WRF_COMPUTE_MAX_TASKS':'4','WRF_COMPUTE_CPU_BUDGET':'4','WRF_COMPUTE_MEMORY_BUDGET':str(2*1024**3)})
        self.env.start();self.addCleanup(self.env.stop)
        self.affinity=patch.object(os,'sched_getaffinity',return_value=set(range(20)),create=True);self.affinity.start();self.addCleanup(self.affinity.stop)
        original=Path.read_text
        self.reader=patch.object(Path,'read_text',lambda path,*a,**kw:'MemAvailable: 67108864 kB\n' if str(path)=='/proc/meminfo' else original(path,*a,**kw));self.reader.start();self.addCleanup(self.reader.stop)
        self.observe=patch.object(p,'inspect',return_value={'status':'running','process':{'pid':4242}});self.inspect=self.observe.start();self.addCleanup(self.observe.stop)
        self.launcher=patch.object(p,'launch_claim',return_value={'status':'running'});self.launch=self.launcher.start();self.addCleanup(self.launcher.stop)
        self.alive=patch.object(p,'process_alive',return_value=False);self.alive.start();self.addCleanup(self.alive.stop)
        self.sessions=patch.object(p,'session_processes',return_value=[]);self.sessions.start();self.addCleanup(self.sessions.stop)

    def run_task(self,n):
        root=self.data/'tasks'/('task-'+str(n))/'control';root.mkdir(parents=True,exist_ok=True)
        return p.pool_launch(self.data,root,format(n,'032x'),'fixture command')

    def test_four_disjoint_allocations_fifth_waits_then_releases(self):
        for n in range(4):self.assertEqual(self.run_task(n)['status'],'running')
        self.assertEqual(self.run_task(4)['status'],'waiting_resources')
        value=json.loads((self.data/'compute-pool.json').read_text())
        allocations=[set(r['cpu_ids']) for r in value['reservations'].values()]
        self.assertEqual(len(set.union(*allocations)),16)
        self.inspect.side_effect=lambda root,token:{'status':'succeeded' if token=='0'*32 else 'running','process':{'pid':4242}}
        self.assertEqual(self.run_task(4)['status'],'running')
        self.assertEqual(p.pool_report(self.data)['released_count'],1)

    def test_unknown_claim_never_replayed_and_blocks_admission(self):
        self.run_task(1);self.inspect.return_value={'status':'unknown'}
        self.assertEqual(self.run_task(1)['status'],'unknown');self.assertEqual(self.launch.call_count,1)
        self.assertIn('身份',self.run_task(2)['reason'])

    def test_memory_and_cpu_shortage_wait_without_claim(self):
        with patch.dict(os.environ,{'WRF_COMPUTE_MEMORY_BUDGET':str(40*1024**3)}):
            self.run_task(1);self.assertIn('内存',self.run_task(2)['reason'])
        self.assertEqual(self.launch.call_count,1)
        with patch.dict(os.environ,{'WRF_COMPUTE_CPU_BUDGET':'19'}):self.assertIn('CPU',self.run_task(3)['reason'])

    def test_terminal_with_live_session_keeps_reservation(self):
        self.run_task(1);self.inspect.return_value={'status':'succeeded','process':{'pid':4242}}
        with patch.object(p,'session_processes',return_value=[{'pid':4243}]):
            self.assertEqual(len(p.pool_report(self.data)['active']),1)
        self.assertEqual(len(p.pool_report(self.data)['active']),0)

    def test_uncertain_write_retains_original_claim(self):
        self.launch.side_effect=RuntimeError('lost launch response')
        with self.assertRaises(RuntimeError):self.run_task(1)
        self.inspect.return_value={'status':'unknown'}
        self.assertEqual(self.run_task(1)['status'],'unknown');self.assertEqual(self.launch.call_count,1)

    def test_path_replacement_and_unverified_cancel_fail_closed(self):
        self.run_task(1)
        self.inspect.return_value={'status':'cancelled'}
        self.assertIn('身份',self.run_task(2)['reason'])
        root=self.data/'tasks/task-1/control';root.rename(root.with_name('original'));root.mkdir()
        with self.assertRaisesRegex(RuntimeError,'directory changed'):p.pool_report(self.data)

    def test_cancelled_attempt_releases_only_after_verified_session_exit(self):
        self.run_task(1)
        root=self.data/'tasks/task-1/control'
        self.inspect.return_value={'status':'cancelled','process':{'pid':4242},'cancellation_verified':True}
        with patch.object(p,'session_processes',return_value=[{'pid':4243}]):
            state=p.release_terminal_reservation(root,format(1,'032x'),dict(self.inspect.return_value))
            self.assertEqual(state['status'],'unknown')
            self.assertEqual(len(p.pool_report(self.data)['active']),1)
        state=p.release_terminal_reservation(root,format(1,'032x'),dict(self.inspect.return_value))
        self.assertTrue(state['compute_resources_released'])
        self.assertEqual(p.pool_report(self.data)['active'],[])

    def test_verified_prelaunch_cancel_releases_but_missing_evidence_keeps_claim(self):
        self.run_task(1);self.inspect.return_value={'status':'cancelled'}
        self.assertEqual(len(p.pool_report(self.data)['active']),1)
        self.inspect.return_value={'status':'cancelled','cancellation_verified':True}
        self.assertEqual(p.pool_report(self.data)['active'],[])

    def test_legacy_terminal_evidence_survives_later_approved_root_removal(self):
        root=self.data/'tasks/legacy/control';root.mkdir(parents=True)
        (self.data/'compute-lease.json').write_text(json.dumps({'root':str(root),'attempt':'a'*32}))
        self.inspect.return_value={'status':'succeeded','process':{'pid':4242}}
        self.run_task(1)
        root.rmdir();root.parent.rmdir()
        self.run_task(2)
        self.assertEqual(self.launch.call_count,2)
        (self.data/'compute-lease.json').write_text(json.dumps({'root':str(root),'attempt':'b'*32}))
        with self.assertRaisesRegex(RuntimeError,'without retained terminal evidence'):self.run_task(3)

    def test_legacy_unknown_or_replaced_identity_remains_blocked(self):
        root=self.data/'tasks/legacy/control';root.mkdir(parents=True)
        (self.data/'compute-lease.json').write_text(json.dumps({'root':str(root),'attempt':'a'*32}))
        self.inspect.return_value={'status':'unknown'}
        self.assertIn('旧发布',self.run_task(1)['reason']);self.launch.assert_not_called()
        self.inspect.return_value={'status':'succeeded','process':{'pid':4242}}
        self.run_task(1);root.rename(root.with_name('original'));root.mkdir()
        with self.assertRaisesRegex(RuntimeError,'Legacy lease directory changed'):self.run_task(2)

    def test_managed_resource_report_retains_legacy_evidence_without_new_launch(self):
        root=self.data/'tasks/legacy/control';root.mkdir(parents=True)
        (self.data/'compute-lease.json').write_text(json.dumps({'root':str(root),'attempt':'a'*32}))
        self.inspect.return_value={'status':'succeeded','process':{'pid':4242}}
        self.assertFalse(p.pool_report(self.data,inspect_legacy=True)['legacy_blocked'])
        self.launch.assert_not_called()
        root.rmdir();root.parent.rmdir()
        self.assertFalse(p.pool_report(self.data,inspect_legacy=True)['legacy_blocked'])
