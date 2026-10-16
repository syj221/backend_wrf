import importlib.util
import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from config import Settings
from hpc_transport import HpcClient


MODULE_PATH = Path(__file__).parent / "scripts" / "forcing_pool_queue.py"
SPEC = importlib.util.spec_from_file_location("forcing_pool_queue_resilience", MODULE_PATH)
queue_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(queue_module)


def queue_args(root: Path) -> Namespace:
    return Namespace(
        source="gfs", root=str(root), min_bytes=8, min_speed=1024,
        request_interval=0, retry_max_attempts=8, retry_max_seconds=900,
        publish_probe_seconds=300, history_retention_days=30,
        ec_template="", action="worker", cycle=None, hours=None,
        priority="prefetch",
    )


class PoolResilienceTest(unittest.TestCase):
    def test_download_progress_is_scoped_and_redacts_raw_errors(self):
        client = HpcClient(Settings())
        state = {"cycles": {"2026100900": {"files": {
            "000": {"status": "queued"},
            "003": {"status": "failed", "error": "Bearer secret /mnt/private", "failure_class": "network"},
            "006": {"status": "downloading"}}}}}
        with patch.object(client, "_pool_queue_state", return_value=state):
            self.assertEqual(client.forcing_download_progress("ecmwf", "2026100900", [0])["status"], "queued")
            result = client.forcing_download_progress("ecmwf", "2026100900", [3])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failed_hours"], [3])
            self.assertNotIn("secret", str(result))
            self.assertNotIn("/mnt", str(result))
            self.assertEqual(client.forcing_download_progress("ecmwf", "2026100900", [9])["untracked_hours"], [9])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(queue_module, "LOCK_ROOT", Path(directory.name) / "locks")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_incomplete_cycle_does_not_hash_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = queue_module.PoolQueue(queue_args(root))
            first = queue.file_path("2026091200", 0)
            first.parent.mkdir(parents=True)
            first.write_bytes(b"GRIBpayload7777")

            with patch.object(queue, "digest", side_effect=AssertionError("must not hash an incomplete cycle")):
                queue._write_manifest_if_complete("2026091200")

            self.assertFalse((first.parent / "manifest.json").exists())

    def test_failure_classification_distinguishes_transient_and_permanent_errors(self):
        queue = queue_module.PoolQueue(queue_args(Path(tempfile.gettempdir())))
        self.assertEqual(queue.classify_error(0, "curl: (6) Could not resolve host")[0], "network")
        self.assertTrue(queue.classify_error(503, "upstream unavailable")[1])
        self.assertEqual(queue.classify_error(404, "HTTP 404")[0], "upstream_permanent")
        self.assertFalse(queue.classify_error(0, "SyntaxError: incompatible Python")[1])

    def test_recovery_only_requeues_interrupted_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = queue_module.PoolQueue(queue_args(root))
            queue.save_state({
                "version": 1,
                "source": "gfs",
                "cycles": {
                    "2026091200": {
                        "active_file": "gfs.t00z.pgrb2.0p25.f000",
                        "files": {"000": {"status": "downloading"}},
                    },
                    "2026091100": {
                        "active_file": None,
                        "files": {"000": {"status": "ready"}},
                    },
                },
            })
            queue.recover_interrupted_downloads()
            state = queue.load_state()
            self.assertEqual(state["cycles"]["2026091200"]["files"]["000"]["status"], "queued")
            self.assertIsNone(state["cycles"]["2026091200"]["active_file"])
            self.assertEqual(state["cycles"]["2026091100"]["files"]["000"]["status"], "ready")

    def test_forgetting_a_cleaned_cycle_removes_queue_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = queue_module.PoolQueue(queue_args(root))
            queue.enqueue("2026091000", [0], "prefetch")
            queue.forget("2026091000")
            self.assertNotIn("2026091000", queue.load_state()["cycles"])

    def test_expired_retry_wait_is_requeued_by_worker_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = queue_module.PoolQueue(queue_args(root))
            queue.save_state({
                "version": 1,
                "source": "gfs",
                "cycles": {
                    "2026091200": {
                        "publication": "published",
                        "files": {
                            "000": {
                                "status": "retry_wait",
                                "priority": "task",
                                "terminal_retry_at": 1,
                                "attempts": 8,
                                "no_progress_attempts": 8,
                            },
                        },
                    },
                },
            })
            self.assertEqual(queue._pick_next(), ("2026091200", 0))
            entry = queue.load_state()["cycles"]["2026091200"]["files"]["000"]
            self.assertEqual(entry["status"], "queued")
            self.assertEqual(entry["attempts"], 0)
            self.assertEqual(entry["no_progress_attempts"], 0)

    def test_hpc_log_classification_handles_legacy_failure_logs(self):
        self.assertEqual(
            HpcClient._failure_class(-1, "SyntaxError: future feature annotations is not defined")[0],
            "runtime_compatibility",
        )
        self.assertEqual(
            HpcClient._failure_class(-1, "curl: (6) Could not resolve host")[0],
            "network",
        )

    def test_cleanup_retains_newest_complete_fallback_cycles(self):
        cycles = [
            {"cycle": f"202609{day:02d}00", "complete": True}
            for day in range(8, 14)
        ]
        self.assertEqual(
            HpcClient._retained_complete_cycles(cycles, 2),
            {"2026091300", "2026091200"},
        )

    def test_incomplete_targets_do_not_block_old_cycle_cleanup_for_both_sources(self):
        cycles = ["2026091400", "2026091300"] + [f"202609{day:02d}00" for day in range(12, 7, -1)]
        settings = replace(Settings(), hpc_gfs_retained_cycles=2)

        class FakeHpcClient(HpcClient):
            def __init__(self, configured_settings, source_output):
                super().__init__(configured_settings)
                self.source_output = source_output

            def _pool_queue_state(self, _root):
                return {}

            def run(self, _command, timeout=120):
                return self.source_output

        for source, complete_count in (("gfs", 73), ("ecmwf", 25)):
            lines = ["ROOT|/pool"]
            for cycle in cycles:
                if cycle == "2026091400":
                    lines.append(f"{cycle}|partial|0|0|0|0|0|0|")
                else:
                    lines.append(f"{cycle}|ready|{complete_count}|100|72|0|0|0|")
            client = FakeHpcClient(settings, "\n".join(lines))
            pool = (
                client.gfs_pool_items(["2026091400", "2026091300"], set())[0]
                if source == "gfs"
                else client.forcing_pool_items(source, ["2026091400", "2026091300"], set())[0]
            )
            self.assertEqual(
                pool["retained_complete_cycles"], ["2026091300", "2026091200"],
            )
            self.assertEqual(
                pool["auto_cleanup_candidates"],
                [f"/pool/202609{day:02d}00" for day in range(11, 7, -1)],
            )

    def test_cleanup_uses_the_managed_queue_without_shell_deletion(self):
        path = "/pool/2026091000"
        settings = replace(Settings(), hpc_gfs_retained_cycles=2)

        class FakeCleanupClient(HpcClient):
            def __init__(self, configured_settings):
                super().__init__(configured_settings)
                self.command = ""

            def gfs_pool_items(self, _targets, _protected):
                return [{
                    "remote_root": "/pool",
                    "cleanup_candidates": [path],
                    "cycles": [{"remote_path": path, "complete": True}],
                }]

            def run(self, command, timeout=120):
                self.command = command
                return f"DELETED|{path}"

        client = FakeCleanupClient(settings)
        result = client.cleanup_gfs_cycles([path], ["2026091400"], set())
        self.assertEqual(result["deleted"], [path])
        self.assertIn("forcing_pool_queue.py cleanup", client.command)
        self.assertNotIn("rm -rf", client.command)
        self.assertIn("--cycle 2026091000", client.command)


if __name__ == "__main__":
    unittest.main()
