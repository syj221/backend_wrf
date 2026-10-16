"""Measured progress regressions; isolated SQLite and mocked remote reads."""
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from config import settings
from database import TaskStore
from task_manager import WrfTaskManager, observed_model_progress


TASK = "wrf_gfs_20261006T000000Z_aaaaaaaa"
TOKEN = "a" * 32
REQUEST = {"start_time": "2026-10-06T00:00:00Z", "end_time": "2026-10-06T01:00:00Z", "spinup": {"hours": 0}}


def stage(name, status="running"):
    return "[stage-status] " + json.dumps({"stage": name, "status": status, "message": "/private/path"})


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.settings = replace(settings, database_path=root / "tasks.sqlite3", run_dir=root / "runs",
                                runtime_asset_root=None, legacy_asset_manifest=None, hpc_poll_seconds=0)
        self.store = TaskStore(self.settings.database_path)
        self.addCleanup(self.store.close)
        self.store.create(TASK, REQUEST, "owner")
        self.store.update(TASK, status="running", runtime={"launch_attempt": TOKEN})
        self.hpc = Mock()
        self.manager = WrfTaskManager(self.settings, self.store, None, self.hpc)
        self.manager._log = Mock()

    def observe(self, log="", status="running", **extra):
        self.assertTrue(self.manager._persist_observed_progress(TASK, {"status": status, "log": log, "attempt": TOKEN, **extra}, (1, TOKEN)))
        return self.store.get(TASK)["runtime"]

    def test_wps_has_stage_without_a_fake_percent(self):
        value = self.observe(stage("prepare") + "\n" + stage("wps"))
        self.assertEqual(value["current_stage"], "wps")
        self.assertEqual(value["current_stage_status"], "running")
        self.assertNotIn("model_progress", value)
        self.assertNotIn("message", value)

    def test_timing_without_stage_supports_legacy_logs_and_is_bounded(self):
        value = self.observe("Timing for main: time 2026-10-06_00:30:00 on domain 1")
        self.assertEqual(value["current_model_time"], "2026-10-06T00:30:00Z")
        self.assertEqual(value["model_progress"], 50)
        self.assertEqual(value["current_stage"], "wrf")
        value = self.observe("Timing for main: time 2026-10-07_00:00:00")
        self.assertEqual(value["model_progress"], 100)

    def test_spinup_is_part_of_the_actual_model_window(self):
        task = {"request": {**REQUEST, "spinup": {"hours": 3}}, "runtime": {}}
        result = observed_model_progress(task, {"log": "Timing for main: time 2026-10-05_23:00:00"})
        self.assertEqual(result["model_progress"], 50)
        task["runtime"]["model_start_time"] = "2026-10-05T21:00:00Z"
        self.assertEqual(observed_model_progress(task, {"log": "Timing for main: time 2026-10-05_23:00:00"})["model_progress"], 50)

    def test_old_tails_cannot_regress_stage_or_model_time(self):
        self.observe(stage("wrf") + "\nTiming for main: time 2026-10-06_00:45:00")
        value = self.observe(stage("wps") + "\nTiming for main: time 2026-10-06_00:15:00")
        self.assertEqual(value["current_stage"], "wrf")
        self.assertEqual(value["model_progress"], 75)

    def test_invalid_or_missing_logs_do_not_invent_progress(self):
        value = self.observe('[stage-status] {bad}\n' + stage("arbitrary") + "\nTiming for main: time 2026-99-99_99:99:99")
        self.assertEqual(value, {"launch_attempt": TOKEN})
        self.assertEqual(self.observe(), value)

    def test_terminal_keeps_measured_time_without_claiming_100_percent(self):
        self.observe("Timing for main: time 2026-10-06_00:30:00")
        value = self.observe(stage("failed", "error"), status="failed")
        self.assertEqual(value["current_stage"], "failed")
        self.assertEqual(value["model_progress"], 50)
        value = self.observe(status="succeeded")
        self.assertEqual(value["current_stage"], "done")
        self.assertEqual(value["model_progress"], 50)

    def test_different_attempt_or_terminal_local_record_is_not_modified(self):
        before = self.store.get(TASK)
        self.assertFalse(self.manager._persist_observed_progress(TASK, {"attempt": "b" * 32, "log": stage("wrf")}, (1, TOKEN)))
        self.assertEqual(self.store.get(TASK), before)
        self.store.begin_attempt(TASK, REQUEST)
        self.assertFalse(self.manager._persist_observed_progress(TASK, {"attempt": TOKEN, "log": stage("wrf")}, (1, TOKEN)))
        self.store.update(TASK, status="succeeded")
        before = self.store.get(TASK)
        self.assertFalse(self.manager._persist_observed_progress(TASK, {"log": stage("failed", "error")}, (2, None)))
        self.assertEqual(self.store.get(TASK), before)

    def test_monitor_persists_observation_before_success(self):
        self.hpc.status.side_effect = [
            {"status": "running", "attempt": TOKEN, "log": stage("wps")},
            {"status": "running", "attempt": TOKEN, "log": "Timing for main: time 2026-10-06_00:30:00"},
            {"status": "succeeded", "attempt": TOKEN, "log": stage("done", "done") + "\nTiming for main: time 2026-10-06_01:00:00"},
        ]
        self.assertTrue(self.manager._monitor(TASK))
        value = self.store.get(TASK)["runtime"]
        self.assertEqual(value["current_stage"], "done")
        self.assertEqual(value["model_progress"], 100)
        self.hpc.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
