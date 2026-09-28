import unittest
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from hpc import build_task_config, _runtime_environment
from config import Settings
from gfs import GfsManager
from schemas import WrfTaskCreate
from task_manager import WrfTaskManager
from catalog_publisher import _asset_rows


def request(source="gfs", interval=1):
    return {
        "data_source": source,
        "start_time": "2026-09-05T06:00:00Z",
        "end_time": "2026-09-05T12:00:00Z",
        "center": {"lat": 35, "lon": 110},
        "forecast_interval_hours": interval,
        "domains": [
            {"id": "d01", "dx": 9000, "dy": 9000, "e_we": 100, "e_sn": 100, "parent_id": 0},
        ],
    }


class ForecastSourceTest(unittest.TestCase):
    def test_ten_minute_catalog_times_are_not_rounded_to_hours(self):
        manifest = {"start_time": "2026-09-28T00:00:00Z", "domains": [{
            "id": "d04", "dx": 1000, "dy": 1000, "grid": [61, 61],
            "variables": [{"name": "T2", "units": "K", "frames": [
                {"time": "2026-09-28T00:10:00Z", "url": "/data/WRF/example/10.webp"},
                {"time": "2026-09-28T01:00:00Z", "url": "/data/WRF/example/60.webp"},
            ]}]}]}
        rows = _asset_rows("fixture", "fixture-task", manifest, datetime.now(timezone.utc))
        self.assertEqual(rows[0]["valid_time"].minute, 10)
        self.assertIsNone(rows[0]["forecast_hour"])
        self.assertEqual(json.loads(rows[0]["extra_json"])["forecast_lead_seconds"], 600)
        self.assertEqual(rows[1]["forecast_hour"], 1)
        self.assertEqual(rows[0]["dx_m"], 1000)

    def test_runtime_time_step_divides_both_output_and_boundary_intervals(self):
        script = (Path(__file__).parent / "scripts" / "wrf.sh").read_text(encoding="utf-8")
        function = re.search(r"select_time_step_for_forcing\(\) \{.*?^\}", script, re.S | re.M).group(0)
        # Execute only the pure arithmetic helper, never source/run the HPC script.
        result = subprocess.run(["bash", "-c", function + "\nselect_time_step_for_forcing 27000 600"],
                                capture_output=True, text=True, check=True, timeout=5)
        step = int(result.stdout.strip())
        self.assertLessEqual(step, 135)
        self.assertEqual(600 % step, 0)
        self.assertEqual(10800 % step, 0)
        self.assertIn('WRF_HISTORY=$(csv_repeat "$WRF_HISTORY_INTERVAL_MINUTES")', script)
        self.assertRegex(script, r"history_interval\s*=\s*\$\{WRF_HISTORY\}")

    def test_gfs_and_ecmwf_are_the_only_task_sources(self):
        self.assertEqual(WrfTaskCreate.model_validate(request()).data_source, "gfs")
        self.assertEqual(WrfTaskCreate.model_validate(request("ec", 3)).data_source, "ecmwf")
        with self.assertRaises(ValueError):
            WrfTaskCreate.model_validate(request("era5"))

    def test_runtime_bundle_uses_forecast_pool_only(self):
        payload = WrfTaskCreate.model_validate(request()).model_dump(mode="json")
        config = build_task_config("wrf_gfs_20260905T060000Z_12345678", payload, "2026090500", [0, 1])
        self.assertIn("gfs_cache", config)
        self.assertNotIn("era5_cache", config)

    def test_boundary_and_output_intervals_are_distinct(self):
        value = request("ecmwf", 3)
        value["history_interval_minutes"] = 10
        payload = WrfTaskCreate.model_validate(value).model_dump(mode="json")
        config = build_task_config("wrf_ecmwf_20260905T060000Z_12345678", payload, "2026090500", [0, 3])
        env = _runtime_environment(config)
        self.assertEqual(env["WRF_FORECAST_FILE_INTERVAL_HOURS"], "3")
        self.assertEqual(env["WRF_HISTORY_INTERVAL_MINUTES"], "10")
        with self.assertRaises(ValueError):
            WrfTaskCreate.model_validate({**value, "history_interval_minutes": 7})
        with self.assertRaisesRegex(ValueError, "整数倍"):
            WrfTaskCreate.model_validate({**request(), "history_interval_minutes": 180,
                                          "end_time": "2026-09-05T11:00:00Z"})

    def test_reference_grid_point_reaches_runtime_and_old_tasks_stay_centred(self):
        for anchor in ({}, {"grid_x": 13.7, "grid_y": 13.2}):
            value = request()
            value["center"].update(anchor)
            payload = WrfTaskCreate.model_validate(value).model_dump(mode="json")
            config = build_task_config("wrf_gfs_20260905T060000Z_12345678", payload, "2026090500", [0, 1])
            env = _runtime_environment(config)
            self.assertEqual(float(env["WRF_REF_X"]), anchor.get("grid_x", 50.5))
            self.assertEqual(float(env["WRF_REF_Y"]), anchor.get("grid_y", 50.5))

    def test_task_ids_reject_era5_source(self):
        with self.assertRaises(ValueError):
            WrfTaskManager.new_task_id("era5", datetime.now(timezone.utc))

    def test_public_wrf_pool_contract_has_only_forecast_sources(self):
        main = (Path(__file__).parent / "main.py").read_text(encoding="utf-8")
        self.assertIn('"data_sources": ["gfs", "ecmwf"]', main)
        self.assertNotIn('"providers": {"gfs": gfs, "ecmwf": ec, "era5": era5}', main)

    def test_ec_downloader_keeps_partial_files_for_resume(self):
        script = (Path(__file__).parent / "scripts" / "download_ecmwf_00z.sh").read_text(encoding="utf-8")
        queue = (Path(__file__).parent / "scripts" / "forcing_pool_queue.py").read_text(encoding="utf-8")
        self.assertIn("forcing_pool_queue.py", script)
        self.assertIn("hour+=3", script)
        self.assertIn('"--continue-at", "-"', queue)
        self.assertIn('startswith("retry-after:")', queue)
        self.assertIn(".pool-queue.json", queue)

    def test_task_falls_back_to_newest_complete_cycle_that_covers_window(self):
        class CompletePool:
            def complete_forcing_cycles(self, _source):
                return ["2026090400"]

        manager = object.__new__(WrfTaskManager)
        manager.gfs = GfsManager(Settings())
        manager.hpc = CompletePool()
        model_start = datetime(2026, 9, 5, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        cycle, hours = manager._select_pool_cycle(
            "ecmwf", model_start, end, 3, "2026090500",
        )
        self.assertEqual(cycle, "2026090400")
        self.assertEqual(hours, [24, 27, 30, 33, 36, 39, 42])


if __name__ == "__main__":
    unittest.main()
