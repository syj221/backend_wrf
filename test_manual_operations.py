"""Manual workbench actions reuse the existing executor and safety boundaries."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from database import TaskStore
from recovery import RecoveryService
from test_guidance_recovery import request
from test_recovery_diagnosis import runtime
from task_manager import WrfTaskManager, TaskConflictError


class ManualOperationsTest(TestCase):
    def setUp(self):
        self.store = TaskStore(Path(':memory:'))
        self.addCleanup(self.store.close)
        self.store.create('task', request(), 'alice')
        self.store.update('task', status='cancelled', stage='cancelled')
        self.manager = SimpleNamespace(store=self.store, get=self.store.get,
            require_operation=Mock(), restart_plan=Mock(return_value={
                'can_restart': True, 'local_paths': [], 'remote_paths': []}),
            restart=Mock(), resume_external=Mock(), retry_outputs=Mock(), render_partial=Mock())
        self.service = RecoveryService(self.manager, runtime)

    def plan(self, action='restart'):
        return self.service.plan('task', None, action, actor_sub='alice', purpose='manual')

    def test_cancelled_without_incident_same_parameters_executes_once(self):
        self.assertEqual(self.store.incidents('task'), [])
        plan = self.plan()
        self.assertIsNone(plan['event_id'])
        self.service.execute('task', plan['id'], 'alice')
        self.service.execute('task', plan['id'], 'alice')
        self.manager.restart.assert_called_once()
        self.assertEqual(self.store.incidents('task'), [])

    def test_invalid_vertical_restart_preserves_original_attempt_and_history(self):
        before = deepcopy(self.store.get('task'))
        proposed = deepcopy(before['request'])
        proposed['vertical_levels'] = 20
        with self.assertRaisesRegex(ValueError, 'not enough eta levels'):
            self.service.plan('task', None, 'restart', proposed,
                              actor_sub='alice', purpose='manual')
        self.assertEqual(self.store.get('task'), before)
        self.assertEqual(self.store.incidents('task'), [])
        self.manager.restart.assert_not_called()

    def test_changed_parameters_are_bound_to_the_plan_and_executor(self):
        proposed = deepcopy(self.store.get('task')['request'])
        proposed['end_time'] = '2026-09-30T09:00:00Z'
        plan = self.service.plan('task', None, 'restart', proposed,
                                 actor_sub='alice', purpose='manual')
        self.assertIn('end_time', plan['changes'])
        self.assertEqual(self.store.get('task')['request']['end_time'], request()['end_time'])
        self.service.execute('task', plan['id'], 'alice')
        self.assertEqual(self.manager.restart.call_args.args[1]['end_time'], proposed['end_time'])
        self.assertEqual(self.manager.restart.call_args.kwargs['confirm_attempt'], 1)

    def test_concurrent_manual_plans_only_claim_once(self):
        from concurrent.futures import ThreadPoolExecutor
        plans = [self.plan(), self.plan()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: self.store.claim_recovery(p['id']), plans))
        self.assertEqual(sorted(results), [False, True])

    def test_unknown_cause_does_not_authorize_unknown_process(self):
        self.manager.restart_plan.return_value['can_restart'] = False
        with self.assertRaises(ValueError):
            self.plan()
        self.manager.restart.assert_not_called()

    def test_other_actor_expired_or_changed_state_cannot_execute(self):
        plan = self.plan()
        with self.assertRaises(ValueError):
            self.service.execute('task', plan['id'], 'bob')
        self.store.update('task', stage='different')
        with self.assertRaises(ValueError):
            self.service.execute('task', plan['id'], 'alice')
        self.manager.restart.assert_not_called()

    def test_manual_and_repair_claims_share_state_fence(self):
        first = self.plan()
        second = deepcopy(first)
        second.update(id='repair-plan', purpose='repair', event_id='incident')
        self.store.save_recovery(second)
        self.assertTrue(self.store.claim_recovery(first['id']))
        self.assertFalse(self.store.claim_recovery(second['id']))

    def test_expired_plan_cannot_execute(self):
        plan = self.plan()
        expired = deepcopy(plan)
        expired.update(id='expired-plan', expires_at=0)
        self.store.save_recovery(expired)
        with self.assertRaises(ValueError):
            self.service.execute('task', expired['id'], 'alice')
        self.manager.restart.assert_not_called()

    def test_new_attempt_is_not_blocked_by_previous_operation(self):
        first = self.plan()
        self.assertTrue(self.store.claim_recovery(first['id']))
        self.store.finish_recovery(first['id'], 'accepted')
        self.store.update('task', attempt_no=2)
        second = self.plan()
        self.assertTrue(self.store.claim_recovery(second['id']))

    def test_partial_render_reuses_executor_without_restart(self):
        plan = self.plan('render_partial')
        self.service.execute('task', plan['id'], 'alice')
        self.manager.render_partial.assert_called_once_with('task')
        self.manager.restart.assert_not_called()

    def test_ambiguous_execution_is_not_replayed(self):
        self.manager.retry_outputs.side_effect = TimeoutError('response lost')
        plan = self.plan('retry_outputs')
        with self.assertRaises(TimeoutError):
            self.service.execute('task', plan['id'], 'alice')
        self.assertEqual(self.service.execute('task', plan['id'], 'alice')['status'], 'unknown')
        self.manager.retry_outputs.assert_called_once()

    def test_publish_reuses_publisher_once_and_rejects_partial_results(self):
        import threading
        manager = object.__new__(WrfTaskManager)
        manager.store = self.store
        lock = threading.RLock()
        manager._operation_lock = lambda _: lock
        manager._require_recovery_idle = Mock()
        manager._publish_catalog_result = Mock(return_value={'status': 'success'})
        self.store.update('task', status='partial_success', result={'meta_json': {}})
        with self.assertRaises(TaskConflictError):
            manager.retry_catalog_publish('task')
        self.store.update('task', status='succeeded', result={'meta_json': {}})
        manager.retry_catalog_publish('task')
        manager.retry_catalog_publish('task')
        manager._publish_catalog_result.assert_called_once()
        manager._require_recovery_idle.side_effect = TaskConflictError('deletion in progress')
        with self.assertRaises(TaskConflictError):
            manager.retry_catalog_publish('task')

    def test_eligibility_partial_and_publish_are_distinct(self):
        manager = object.__new__(WrfTaskManager)
        manager.store = self.store
        self.store.update('task', status='failed', runtime={
            'remote_wrf_succeeded': True, 'forcing_cycle': '2026092900',
            'output_validation': {'invalid': ['frame']}}, result={'meta_json': {}})
        ops = {o['action']: o for o in manager.operations('task')}
        self.assertTrue(ops['render_partial']['enabled'])
        self.assertFalse(ops['restart']['enabled'])
        self.assertFalse(ops['publish']['enabled'])

class PrelaunchCancellationProofTest(TestCase):
    def test_tombstone_is_terminal_only_without_conflicting_launch_evidence(self):
        import json
        import subprocess
        import sys
        from tempfile import TemporaryDirectory
        from terminal_evidence import WAITING_PROBE
        with TemporaryDirectory(prefix='wrf-terminal-proof-') as directory:
            control = Path(directory)
            token = 'a' * 32
            attempt = control / 'launches' / token
            attempt.mkdir(parents=True)
            (attempt / 'state.json').write_text(json.dumps({'attempt': token,
                'status': 'failed', 'cancelled': True, 'cancellation_verified': True,
                'reason': 'cancelled_before_launch'}))
            proc = control / 'fakeproc'
            proc.mkdir()
            probe = WAITING_PROBE.replace("Path('/proc')", "Path(" + repr(str(proc)) + ")")
            args = [sys.executable, '-B', '-c', probe, 'managed', str(control),
                    json.dumps({'launch_attempt': token, 'remote_process': None})]
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['never_started'])
            (control / 'service.pid').write_text('123')
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('conflicts with launch evidence', result.stderr)
