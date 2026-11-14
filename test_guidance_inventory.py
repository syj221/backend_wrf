"""Inventory and integrity are different read-only observations."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from guidance import recommendations


class GuidanceInventoryTest(unittest.TestCase):
    now = datetime(2026, 10, 30, 2, 17, tzinfo=timezone.utc)
    request = {
        "configuration_profile": "wrf-ordinary-ui-2",
        "data_source": "gfs",
        "duration_hours": 12,
        "spinup": {"mode": "custom", "hours": 6},
    }

    def pool(self, **changes):
        cycle = {
            "cycle": "2026102800",
            "complete": True,
            "status": "ready",
            "available_hours": list(range(73)),
        }
        cycle.update(changes)
        return {
            "status": "ok",
            "pool_items": [
                {"provider": "gfs", "cycles": [cycle]},
            ],
        }

    def test_inventory_is_cached_but_not_integrity_verified(self):
        pool = self.pool()
        before = deepcopy(pool)
        result = recommendations(self.request, {}, pool, self.now)
        windows = [
            c["data_window"]
            for c in result["suggestions"]["time"]["candidates"]
        ]
        self.assertTrue(windows)
        for window in windows:
            self.assertEqual(window["cycle"], "2026102800")
            self.assertEqual(window["availability_status"], "present")
            self.assertEqual(window["integrity_status"], "unverified")
            self.assertEqual(window["status"], "unknown")
        self.assertEqual(pool, before)

    def test_known_integrity_failure_cannot_be_ready_or_selected_fallback(
        self,
    ):
        for changes in (
            {"failure_class": "data_integrity"},
            {"integrity_status": "invalid"},
            {"available_hours": [h for h in range(73) if h != 54]},
        ):
            with self.subTest(changes=changes):
                result = recommendations(
                    self.request, {}, self.pool(**changes), self.now
                )
                for candidate in result["suggestions"]["time"]["candidates"]:
                    self.assertNotEqual(
                        candidate["data_window"]["cycle"], "2026102800"
                    )
                    self.assertNotEqual(
                        candidate["data_window"]["status"], "ready"
                    )

    def test_failed_pool_read_does_not_promote_cached_inventory(self):
        pool = {**self.pool(), "status": "unavailable"}
        result = recommendations(self.request, {}, pool, self.now)
        for candidate in result["suggestions"]["time"]["candidates"]:
            self.assertEqual(candidate["data_window"]["status"], "unknown")
            self.assertNotEqual(
                candidate["data_window"]["cycle"], "2026102800"
            )

    def test_verified_hours_require_whole_window_and_matching_inventory(self):
        for hours, expected in ((list(range(73)), "ready"), ([24], "unknown")):
            result = recommendations(
                self.request, {}, self.pool(valid_hours=hours), self.now
            )
            window = result["suggestions"]["time"]["candidates"][0][
                "data_window"
            ]
            self.assertEqual(window["status"], expected)

    def test_old_cycle_outside_full_spinup_window_is_not_offered(self):
        result = recommendations(
            self.request, {}, self.pool(cycle="2026102600"), self.now
        )
        for candidate in result["suggestions"]["time"]["candidates"]:
            self.assertNotEqual(
                candidate["data_window"]["cycle"], "2026102600"
            )
            self.assertNotEqual(candidate["data_window"]["status"], "ready")

    def test_malformed_hours_and_nonboolean_complete_never_prove_ready(self):
        from forcing_readiness import observe_cycle

        for value in ([True], [1, 1], ["1"], [73], None):
            with self.subTest(value=value):
                cycle = {
                    "complete": True,
                    "available_hours": value,
                    "valid_hours": [1],
                }
                self.assertEqual(
                    observe_cycle({"status": "ok"}, cycle, [1])["status"],
                    "unknown",
                )
        cycle = {
            "complete": True,
            "available_hours": [1],
            "valid_hours": [True],
        }
        self.assertEqual(
            observe_cycle({"status": "ok"}, cycle, [1])["status"], "unknown"
        )
        result = recommendations(
            self.request, {}, self.pool(complete="false"), self.now
        )
        for candidate in result["suggestions"]["time"]["candidates"]:
            self.assertNotEqual(
                candidate["data_window"]["cycle"], "2026102800"
            )

    def test_executor_excludes_known_bad_and_unverified_complete_flags(self):
        from unittest.mock import Mock
        from hpc_transport import HpcClient
        from config import settings

        client = HpcClient(settings)
        client.forcing_pool_items = Mock(
            return_value=[
                {
                    "cycles": [
                        {
                            "cycle": "2026103000",
                            "complete": True,
                            "failure_class": "data_integrity",
                        },
                        {
                            "cycle": "2026102900",
                            "complete": True,
                            "integrity_status": "invalid",
                        },
                        {"cycle": "2026102800", "complete": "false"},
                        {"cycle": "2026102700", "complete": True},
                    ]
                }
            ]
        )
        self.assertEqual(client.complete_forcing_cycles("gfs"), ["2026102700"])


if __name__ == "__main__":
    unittest.main()
