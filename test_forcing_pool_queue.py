import importlib.util
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


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
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(queue_module, "LOCK_ROOT", Path(directory.name) / "locks")
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_provider_slot_is_inherited_and_same_source_remains_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = queue_module.PoolQueue(args(root / 'ecmwf'))
            queue.enqueue('2026090800', [3], 'task')
            with patch.object(queue, '_download_one_locked') as download:
                with queue_module.kernel_guard(root / 'ecmwf' / '.pool-download-slot', blocking=False):
                    queue._download_one('2026090800', 3)
                    download.assert_not_called()
                observed = []
                download.side_effect = lambda *unused: observed.append(queue.worker_fds)
                previous = queue.worker_fds
                queue._download_one('2026090800', 3)
                self.assertEqual(len(observed[0]), len(previous) + 1)
                self.assertEqual(queue.worker_fds, previous)

    def test_surviving_child_retains_only_its_provider_slot(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slot = root / 'gfs' / '.pool-download-slot'
            with queue_module.kernel_guard(slot, blocking=False) as guard:
                child = subprocess.Popen([sys.executable, '-c', "import sys;print('ready',flush=True);sys.stdin.read()"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, pass_fds=(guard,))
                self.assertEqual(child.stdout.readline().strip(), b'ready')
            try:
                with queue_module.kernel_guard(slot, blocking=False) as blocked:
                    self.assertIsNone(blocked)
                with queue_module.kernel_guard(root / 'ecmwf' / '.pool-download-slot', blocking=False) as independent:
                    self.assertIsNotNone(independent)
            finally:
                child.communicate(timeout=5)
            with queue_module.kernel_guard(slot, blocking=False) as released:
                self.assertIsNotNone(released)

    def test_other_provider_and_legacy_slot_do_not_block_prefetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gfs = queue_module.PoolQueue(args(root / 'gfs', 'gfs'))
            ec = queue_module.PoolQueue(args(root / 'ecmwf'))
            gfs.enqueue('2026090800', [0], 'prefetch')
            ec.enqueue('2026090700', [3], 'task', 'forcing_' + 'a' * 32)
            with patch.object(gfs, '_download_one_locked') as download:
                with queue_module.kernel_guard(root / '.pool-download-slot', blocking=False):
                    with queue_module.kernel_guard(ec.root / '.pool-download-slot', blocking=False):
                        gfs._download_one('2026090800', 0)
                download.assert_called_once()

    def test_two_providers_overlap_but_cycle_lock_still_blocks(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gfs = queue_module.PoolQueue(args(root / 'gfs', 'gfs'))
            ec = queue_module.PoolQueue(args(root / 'ecmwf'))
            for queue in (gfs, ec):
                queue.enqueue('2026090800', [0], 'task')
            barrier = threading.Barrier(2)
            errors = []
            def downloading(*unused):
                try:
                    barrier.wait(timeout=3)
                except Exception as error:
                    errors.append(error)
            with patch.object(gfs, '_download_one_locked', side_effect=downloading), patch.object(ec, '_download_one_locked', side_effect=downloading):
                threads = [threading.Thread(target=q._download_one, args=('2026090800', 0)) for q in (gfs, ec)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(5)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
            with queue_module.exclusive(gfs.cycle_lock('2026090800'), blocking=False):
                with patch.object(gfs, '_download_one_locked') as download:
                    gfs._download_one('2026090800', 0)
                    download.assert_not_called()
            self.assertEqual(gfs.load_state()['cycles']['2026090800']['wait_code'], 'cycle_busy')

    def test_prefetch_waits_for_f072_publication_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = queue_module.PoolQueue(args(Path(directory)))
            queue.enqueue("2026090800", [0], "prefetch")
            self.assertEqual(queue._pick_next(), ("2026090800", -1))

    def test_due_publication_probe_preempts_older_bulk_but_not_tasks(self):
        for source in ('gfs','ecmwf'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                queue=queue_module.PoolQueue(args(Path(directory),source))
                queue.enqueue('2026101800',[0],'prefetch')
                queue._mark_publication('2026101800','published')
                queue.enqueue('2026101900',[0],'prefetch')
                self.assertEqual(queue._pick_next(),('2026101900',-1))
                queue._mark_publication('2026101900','waiting_publish',queue_module.epoch_now()+300)
                self.assertEqual(queue._pick_next(),('2026101800',0))
                queue._mark_publication('2026101900','waiting_publish',0)
                queue.enqueue('2026101800',[0],'task','forcing_'+'b'*32)
                self.assertEqual(queue._pick_next(),('2026101800',0))

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
