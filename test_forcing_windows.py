"""Profile-specific boundary windows; isolated stores and no remote execution."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from config import settings
from database import TaskStore
from gfs import GfsManager, boundary_buffer_hours
from guidance import recommendations
from hpc import write_task_bundle
from task_manager import WrfTaskManager
from test_wrf_forecast_sources import request


UI2 = "wrf-ordinary-ui-2"
UI1 = "wrf-ordinary-ui-1"
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
START = datetime(2026, 10, 6, tzinfo=timezone.utc)
TASK = "wrf_gfs_20261006T000000Z_aaaaaaaa"


class ForcingWindowTest(unittest.TestCase):
    def setUp(self):
        self.gfs = GfsManager(settings)

    def test_only_ui2_disables_the_historical_buffer(self):
        self.assertEqual(boundary_buffer_hours(UI2), 0)
        for profile in (None, UI1, "unrecognized"):
            self.assertEqual(boundary_buffer_hours(profile), 6)
        self.assertEqual(self.gfs.select_cycle(START, START + timedelta(hours=1), now=NOW)[1], [0, 7])
        self.assertEqual(self.gfs.required_hours(START, START + timedelta(hours=1), 1, "2026100600"), list(range(8)))

    def test_spinup_zero_one_hour_needs_only_two_source_times(self):
        end = START + timedelta(hours=1)
        for source, interval, expected in (("gfs", 1, [0, 1]), ("ecmwf", 3, [0, 3])):
            cycle, bounds = self.gfs.select_cycle(START, end, now=NOW, data_source=source, buffer_hours=0)
            self.assertEqual(bounds, [0, 1])
            self.assertEqual(self.gfs.required_hours(START, end, interval, cycle, data_source=source, buffer_hours=0), expected)

    def test_end_is_rounded_up_to_the_next_required_source_time(self):
        start = START + timedelta(hours=1, minutes=30)
        for interval, end, expected in ((1, START + timedelta(hours=2, seconds=1), [1, 2, 3]),
                                        (3, START + timedelta(hours=4), [0, 3, 6]),
                                        (3, START + timedelta(hours=6), [0, 3, 6])):
            self.assertEqual(self.gfs.required_hours(start, end, interval, "2026100600", buffer_hours=0), expected)
        end = START + timedelta(hours=2, seconds=1)
        self.assertEqual(self.gfs.select_cycle(START, end, now=NOW, buffer_hours=0)[1], [0, 3])

    def test_f072_is_available_without_buffer_but_never_exceeded(self):
        end = START + timedelta(hours=72)
        self.assertEqual(self.gfs.select_cycle(START, end, now=NOW, buffer_hours=0)[1], [0, 72])
        self.assertEqual(self.gfs.required_hours(START, end, 3, "2026100600", buffer_hours=0)[-1], 72)
        with self.assertRaises(RuntimeError):
            self.gfs.select_cycle(START, end, now=NOW)
        with self.assertRaises(ValueError):
            self.gfs.required_hours(START, end + timedelta(seconds=1), 3, "2026100600", buffer_hours=0)

    def test_fallback_cycle_uses_the_same_profile_window(self):
        manager = WrfTaskManager(settings, Mock(), self.gfs, Mock())
        manager.hpc.complete_forcing_cycles.return_value = ["2026100500"]
        cycle, hours = manager._select_pool_cycle("ecmwf", START, START + timedelta(hours=4), 3, "2026100600", buffer_hours=0)
        self.assertEqual((cycle, hours), ("2026100500", [24, 27, 30]))
        cycle, hours = manager._select_pool_cycle("gfs", START, START + timedelta(hours=72), 1, "2026100600", buffer_hours=0)
        self.assertEqual((cycle, hours[-1]), ("2026100600", 72))

    def test_actual_preparation_uses_nondefault_spinup_and_no_second_buffer(self):
        for spinup, expected_cycle, expected in ((0, "2026100600", [0, 1]),
                                                 (3, "2026100500", [21, 22, 23, 24, 25]),
                                                 (12, "2026100500", list(range(12, 26)))):
            with self.subTest(spinup=spinup), TemporaryDirectory() as directory:
                root = Path(directory)
                cfg = replace(settings, database_path=root / "tasks.sqlite3", run_dir=root / "runs", data_dir=root / "data",
                              runtime_asset_root=None, legacy_asset_manifest=None)
                store = TaskStore(cfg.database_path)
                try:
                    value = {**request(), "configuration_profile": UI2,
                             "start_time": START.isoformat(), "end_time": (START + timedelta(hours=1)).isoformat(),
                             "spinup": {"mode": "custom", "hours": spinup}}
                    store.create(TASK, value, "alice")
                    hpc = Mock()
                    hpc.complete_forcing_cycles.return_value = []
                    hpc.ensure_remote_forcing.side_effect = RuntimeError("stop before any transfer or launch")
                    manager = WrfTaskManager(cfg, store, self.gfs, hpc)
                    manager._log = Mock()
                    with self.assertRaisesRegex(RuntimeError, "stop before"):
                        manager._execute(TASK)
                    self.assertEqual(hpc.ensure_remote_forcing.call_args.args, ("gfs", expected_cycle, expected))
                    runtime = store.get(TASK)["runtime"]
                    self.assertEqual(runtime["forcing_boundary_buffer_hours"], 0)
                    self.assertEqual(runtime["spinup_hours"], spinup)
                    hpc.prepare_runtime.assert_not_called()
                    hpc.launch.assert_not_called()
                    advisory = recommendations(value, {}, now=NOW)["data_window"]
                    self.assertEqual(advisory["required_hours"], [expected[0], expected[-1]])
                    self.assertEqual(advisory["boundary_buffer_hours"], 0)
                finally:
                    store.close()

    def test_task_bundle_exports_exact_requested_hours_and_expected_index(self):
        value = {**request(), "configuration_profile": UI2, "start_time": START.isoformat(),
                 "end_time": (START + timedelta(hours=1)).isoformat(), "spinup": {"mode": "custom", "hours": 0}}
        entries = [{"name": f"gfs.t00z.pgrb2.0p25.f{hour:03d}", "forecast_hour": hour, "size": 10, "sha256": "a" * 64} for hour in (0, 1)]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_task_bundle(TASK, value, "2026100600", [0, 1], entries,
                              root / "task.json", root / "task.env", root / "forcing.expected.tsv")
            cfg = json.loads((root / "task.json").read_text())
            self.assertEqual(cfg["gfs_cache"]["gfs_forecast_hours"], [0, 1])
            self.assertIn("WRF_GFS_FORECAST_HOURS='000 001'", (root / "task.env").read_text())
            self.assertEqual([line.split("\t")[-1] for line in (root / "forcing.expected.tsv").read_text().splitlines()], ["000", "001"])


if __name__ == "__main__":
    unittest.main()
