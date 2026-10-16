import unittest
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from threading import Barrier
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from config import settings
from database import TaskStore
from guidance import recommendations, configuration_rules
from recovery import RecoveryService, diagnose
from schemas import WrfTaskCreate
from task_manager import WrfTaskManager, RemoteTaskFailure


def request():
    return WrfTaskCreate.model_validate({
        "start_time": "2026-09-29T06:00:00Z", "end_time": "2026-09-30T06:00:00Z",
        "center": {"lat": 30, "lon": 106},
        "domains": [{"id": "d01", "dx": 9000, "dy": 9000, "e_we": 100, "e_sn": 100, "parent_id": 0}],
    }).model_dump(mode="json")


class GuidanceTest(unittest.TestCase):
    def test_partial_cycle_can_cover_the_required_task_window(self):
        payload={"configuration_profile":"wrf-ordinary-ui-2","data_source":"gfs","start_time":"2026-10-07T03:00:00Z","end_time":"2026-10-07T04:00:00Z","spinup":{"mode":"custom","hours":0}}
        pool={"pool_items":[{"provider":"gfs","cycles":[{"cycle":"2026100700","complete":False,"status":"partial","available_hours":list(range(8))}]}]}
        ready=recommendations(payload,{},pool,datetime(2026,10,7,12,tzinfo=timezone.utc))["data_window"]
        self.assertEqual(ready["status"],"ready")
        self.assertEqual(ready["missing_hours"],[])
        pool["pool_items"][0]["cycles"][0]["available_hours"]=[0,1,2]
        missing=recommendations(payload,{},pool,datetime(2026,10,7,12,tzinfo=timezone.utc))["data_window"]
        self.assertEqual(missing["status"],"needs_download")
        self.assertIn(3,missing["missing_hours"])

    def test_user_duration_is_not_replaced_by_twenty_four_hours(self):
        for hours in (6, 18, 36):
            value = recommendations({"duration_hours": hours}, {}, now=datetime(2026, 9, 29, 7, 10, tzinfo=timezone.utc))
            start, end = [datetime.fromisoformat(t.replace("Z", "+00:00")) for t in value["suggestions"]["time"]["value"]]
            self.assertEqual((end-start).total_seconds(), hours * 3600)
    def test_current_forecast_default_and_provider_isolation(self):
        now = datetime(2026, 9, 29, 7, 10, tzinfo=timezone.utc)
        pool = {"pool_items": [{"provider": "gfs", "cycles": [{"cycle": "2026092900", "complete": True}]}]}
        value = recommendations({"data_source": "ecmwf"}, {}, pool, now)
        self.assertEqual(value["suggestions"]["time"]["value"], ["2026-09-29T09:00:00Z", "2026-09-30T09:00:00Z"])
        self.assertNotEqual(value["data_window"]["status"], "ready")
        self.assertEqual(value["data_window"]["required_hours"], [3, 39])
        self.assertIsNone(value["resources"]["memory_gb"])

    def test_explicit_time_is_preserved_and_missing_buffer_detected(self):
        value = recommendations({**request(), "end_time": "2026-10-03T06:00:00Z"}, {}, now=datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(value["data_window"]["status"], "unavailable")
        self.assertEqual(value["suggestions"]["time"]["value"][1], "2026-10-03T06:00:00Z")

    def test_recommendations_are_not_hard_rules(self):
        value = request(); value["domains"][0]["e_we"] = 70
        self.assertTrue(any(r["severity"] == "warning" for r in configuration_rules(value, True)))
        self.assertFalse(any(r["severity"] == "error" for r in configuration_rules(value, True)))
        value["time_step_seconds"] = 100
        self.assertTrue(any(r["severity"] == "error" for r in configuration_rules(value, True)))


class RecoveryTest(unittest.TestCase):
    def test_namelist_read_failure_is_system_configuration_and_requires_new_plan(self):
        evidence = "FIO-F-228/namelist read/unit=10/end of file reached without finding group. File name = 'namelist.wps', record = 43"
        task = self.store.get('task')
        diagnosis = diagnose(task,evidence)
        self.assertEqual(diagnosis['code'],'configuration_generation')
        manager = WrfTaskManager.__new__(WrfTaskManager)
        manager.get = Mock(return_value=task)
        failure = manager._failure_info('task',RemoteTaskFailure({'exit_code':1,'log':evidence}))
        self.assertEqual(failure['failure_class'],'configuration_generation')
        self.assertEqual(failure['recommended_action'],'regenerate_plan')
        self.store.update('task',failure=failure)
        event = self.store.incidents('task')[-1]
        with self.assertRaisesRegex(ValueError,'修正配置'):
            self.service.plan('task',event['id'],'restart')
        self.manager.restart.assert_not_called()

    def setUp(self):
        self.store = TaskStore(Path(":memory:"))
        self.addCleanup(self.store.close)
        self.store.create("task", request(), "alice")
        self.store.update("task", status="waiting_restart", stage="failed", failure={"failure_class": "model", "message": "model failed", "evidence": "points exceeded cfl"})
        self.manager = SimpleNamespace(store=self.store, get=self.store.get,
            restart_plan=Mock(return_value={"can_restart": True, "local_paths": ["/fixture/run"], "remote_paths": ["/fixture/remote"]}),
            restart=Mock(), resume_external=Mock(), retry_outputs=Mock())
        self.service = RecoveryService(self.manager)
        self.event = self.store.incidents("task")[0]

    def test_cfl_plan_is_readonly_and_execution_is_single_use(self):
        plan = self.service.plan("task", self.event["id"], "reduce_time_step")
        self.manager.restart.assert_not_called()
        self.assertLess(plan["request"]["time_step_seconds"], 45)
        self.assertEqual(self.service.execute("task", plan["id"])["status"], "accepted")
        self.service.execute("task", plan["id"])
        self.manager.restart.assert_called_once()

    def test_duplicate_plans_for_one_event_cannot_both_execute(self):
        a = self.service.plan("task", self.event["id"], "reduce_time_step")
        b = self.service.plan("task", self.event["id"], "reduce_time_step")
        self.service.execute("task", a["id"])
        with self.assertRaises(ValueError): self.service.execute("task", b["id"])
        self.manager.restart.assert_called_once()

    def test_independent_connections_claim_one_event_once(self):
        with TemporaryDirectory(prefix="wrf-recovery-claim-") as directory:
            stores = [TaskStore(Path(directory) / "tasks.sqlite3") for _ in range(2)]
            try:
                for identifier in ("a", "b"):
                    stores[0].save_recovery({"id": identifier, "event_id": "event"})
                barrier = Barrier(2)
                def claim(index):
                    barrier.wait()
                    return stores[index].claim_recovery(("a", "b")[index])
                with ThreadPoolExecutor(max_workers=2) as executor:
                    self.assertEqual(sorted(executor.map(claim, (0, 1))), [False, True])
                self.assertFalse(stores[0].claim_recovery("missing"))
            finally:
                for store in stores:
                    store.close()

    def test_changed_or_expired_plan_does_not_execute(self):
        plan = self.service.plan("task", self.event["id"], "reduce_time_step")
        self.store.update("task", stage="changed")
        with self.assertRaises(ValueError): self.service.execute("task", plan["id"])
        self.manager.restart.assert_not_called()

    def test_unknown_result_never_replays(self):
        plan = self.service.plan("task", self.event["id"], "reduce_time_step")
        self.manager.restart.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError): self.service.execute("task", plan["id"])
        self.assertEqual(self.service.execute("task", plan["id"])["status"], "unknown")
        self.manager.restart.assert_called_once()

    def test_read_events_does_not_create_repeated_incidents(self):
        self.service.events("task"); self.service.events("task")
        self.assertEqual(len(self.store.incidents("task")), 1)
        self.assertEqual(self.event["code"], "cfl")
        self.assertEqual(diagnose({"error": "segmentation fault"})["confidence"], "low")

    def test_confirm_policy_pauses_without_scheduling_recovery(self):
        manager = WrfTaskManager(settings, self.store, None, Mock())
        manager._failure_info = Mock(return_value={"failure_class": "external", "message": "connection lost"})
        manager._log = Mock(); manager._schedule_reconcile = Mock()
        manager._defer_external("task", ConnectionError())
        self.assertEqual(self.store.get("task")["status"], "paused_external")
        manager._schedule_reconcile.assert_not_called()
        self.assertEqual(manager.resume_external("task", automatic=True)["status"], "paused_external")

    def test_expired_plan_is_rejected_and_does_not_claim(self):
        from unittest.mock import patch
        plan = self.service.plan("task", self.event["id"], "reduce_time_step")
        with patch("recovery.time.time", return_value=plan["expires_at"] + 1):
            with self.assertRaises(ValueError): self.service.execute("task", plan["id"])
        self.assertEqual(self.store.recovery(plan["id"])["status"], "pending")
        self.manager.restart.assert_not_called()

    def test_success_resolves_events_and_new_failure_is_retained(self):
        self.store.update("task", status="succeeded", failure=None)
        self.assertEqual(self.store.incidents("task")[0]["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
