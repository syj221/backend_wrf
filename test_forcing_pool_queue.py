import importlib.util
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "scripts" / "forcing_pool_queue.py"
SPEC = importlib.util.spec_from_file_location("forcing_pool_queue", MODULE_PATH)
queue_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(queue_module)


def args(root: Path, source: str = "ecmwf") -> Namespace:
    return Namespace(
        source=source, root=str(root), min_bytes=8, min_speed=1024,
        request_interval=0, retry_max_attempts=8, retry_max_seconds=900,
        publish_probe_seconds=300, ec_template="", action="worker", cycle=None,
        hours=None, priority="prefetch",
    )


class ForcingPoolQueueTest(unittest.TestCase):
    def test_worker_process_lock_is_single_instance_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / ".pool-worker.lockdir"
            with queue_module.exclusive_process(lock) as first:
                self.assertIsNotNone(first)
                with queue_module.exclusive_process(lock) as second:
                    self.assertIsNone(second)
            self.assertFalse(lock.exists())

    def test_ecmwf_uses_three_hour_fields_only(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = queue_module.PoolQueue(args(Path(directory), "ecmwf"))
            self.assertEqual(queue.expected_hours(), tuple(range(0, 73, 3)))
            with self.assertRaises(ValueError):
                queue.enqueue("2026090800", [1], "prefetch")

    def test_gfs_keeps_hourly_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = queue_module.PoolQueue(args(Path(directory), "gfs"))
            self.assertEqual(queue.expected_hours(), tuple(range(73)))

    def test_task_hours_preempt_prefetch_for_same_source(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = queue_module.PoolQueue(args(Path(directory)))
            queue.enqueue("2026090800", [0, 3], "prefetch")
            queue.enqueue("2026090700", [3], "task")
            self.assertEqual(queue._pick_next(), ("2026090700", 3))

    def test_prefetch_waits_for_f072_publication_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = queue_module.PoolQueue(args(Path(directory)))
            queue.enqueue("2026090800", [0], "prefetch")
            self.assertEqual(queue._pick_next(), ("2026090800", -1))

    def test_existing_valid_file_is_deduplicated_and_hashed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "2026090800" / "ecmwf.t00z.ifs.0p25.f003.grib2"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"GRIBpayload7777")
            queue = queue_module.PoolQueue(args(root))
            queue.enqueue("2026090800", [3], "task")
            entry = queue.load_state()["cycles"]["2026090800"]["files"]["003"]
            self.assertEqual(entry["status"], "ready")
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")

    def test_retry_after_is_honored_with_a_bounded_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            headers = Path(directory) / "headers"
            headers.write_text("HTTP/2 429\r\nRetry-After: 1200\r\n", encoding="iso-8859-1")
            self.assertEqual(queue_module.PoolQueue._retry_after(headers, 1, 900), 900)


if __name__ == "__main__":
    unittest.main()
