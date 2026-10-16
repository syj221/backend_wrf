"""Concurrency regressions using isolated stores and no SSH or real processes."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from config import settings
from database import TaskStore
from scripts import managed_process
from hpc import HpcClient, HpcError
from task_manager import WrfTaskManager, TaskConflictError
from test_namelist_config import request


TASK = "wrf_gfs_20261008T000000Z_aaaaaaaa"
TOKEN = "a" * 32


class ManualTimer:
    def __init__(self, interval, callback):
        self.interval, self.callback = interval, callback
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class TaskManagerSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = replace(settings, database_path=self.root / "tasks.sqlite3",
                              run_dir=self.root / "runs", data_dir=self.root / "outputs",
                              runtime_asset_root=None, legacy_asset_manifest=None,
                              ordinary_max_concurrent=1, hpc_gfs_prefetch_enabled=False)
        self.store = TaskStore(self.config.database_path)
        self.addCleanup(self.store.close)
        self.store.create(TASK, request(), "owner")
        self.hpc = Mock()
        self.hpc.status.return_value = {"status": "failed"}
        self.hpc.task_artifact_paths.return_value = []
        self.manager = WrfTaskManager(self.config, self.store, None, self.hpc)
        self.manager._log = Mock()

    def test_deletion_tombstone_blocks_catalog_retry_and_internal_publication(self):
        self.store.update(TASK,status='succeeded',result={'meta_json':{}})
        with self.store._db:
            self.store._db.execute('INSERT INTO wrf_task_tombstones VALUES (?,?,?,NULL)',(TASK,'isolated-delete','deleting'))
        with patch('task_manager.publish_workbench_result') as publish:
            with self.assertRaisesRegex(TaskConflictError,'删除事务'):
                self.manager.retry_catalog_publish(TASK)
            with self.assertRaisesRegex(TaskConflictError,'删除事务'):
                self.manager._publish_catalog_result(TASK,self.store.get(TASK),{})
            publish.assert_not_called()

    def test_private_task_never_calls_catalog_publisher(self):
        task = self.store.get(TASK)
        task['request']['publish_to_catalog'] = False
        with patch('task_manager.publish_workbench_result') as publish:
            result = self.manager._publish_catalog_result(TASK, task, {})
            self.assertEqual(result['reason'], 'task_publication_disabled')
            publish.assert_not_called()

    def test_edited_restart_archives_failure_log_and_request_for_history(self):
        run=self.config.run_dir/TASK;run.mkdir(parents=True)
        message='real.exe fatal: not enough eta levels to reach p_top\n'
        (run/'service.log').write_text(message)
        original=request();original['vertical_levels']=20
        failure={'failure_class':'model','evidence':message}
        self.store.update(TASK,status='waiting_restart',request=original,failure=failure,error='real.exe exit 1')
        self.manager._dispatch=Mock()
        revised={**original,'vertical_levels':88}
        updated=self.manager.restart(TASK,revised,confirm_task_id=TASK,confirm_attempt=1)
        self.assertEqual(updated['attempt_no'],2)
        archived=self.store.attempts(TASK)[0]
        self.assertEqual(Path(archived['log_path']).read_text(),message)
        snapshot=json.loads((Path(archived['log_path']).parent/'attempt.json').read_text())
        self.assertEqual(snapshot['request']['vertical_levels'],20)
        self.assertEqual(snapshot['failure'],failure)
        self.assertEqual(updated['request']['vertical_levels'],88)
        self.assertIn(message,self.manager.logs(TASK,attempt_no=1)['text'])

    def test_same_attempt_concurrent_restart_only_cleans_once(self):
        self.store.update(TASK, status="waiting_restart")
        entered, release = threading.Event(), threading.Event()

        def status(_task_id):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Restart rendezvous timed out")
            return {"status": "failed"}

        self.hpc.status.side_effect = status
        self.manager._dispatch = Mock()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.manager.restart, TASK, request(),
                                confirm_task_id=TASK, confirm_attempt=1)
            self.assertTrue(entered.wait(3))
            second = pool.submit(self.manager.restart, TASK, request(),
                                 confirm_task_id=TASK, confirm_attempt=1)
            release.set()
            self.assertEqual(first.result(3)["attempt_no"], 2)
            with self.assertRaisesRegex(ValueError, "确认值已过期"):
                second.result(3)
        self.hpc.cleanup_task_attempt.assert_called_once()
        self.assertEqual(self.store.get(TASK)["attempt_no"], 2)
        self.assertEqual(len(self.store.attempts(TASK)), 1)

    def test_release_is_persisted_before_the_first_forcing_operation(self):
        # Explicit fixture paths keep this contract independent of deployment env.
        self.config = replace(self.config, hpc_release_root="/synthetic-runtime",
                              hpc_workbench_data_root="/synthetic-data")
        self.manager.settings = self.config
        self.manager.hpc = HpcClient(self.config)
        self.manager.gfs = Mock()
        def first_forcing(*_args, **_kwargs):
            runtime = self.store.get(TASK)["runtime"]
            self.assertEqual(runtime["release_id"], "release03")
            self.assertEqual(runtime["release_fingerprint"], "fixed-fingerprint")
            self.assertIn("/releases/release03/scripts", self.manager.hpc.service_dir(TASK))
            self.assertNotIn("backend_wrf_tasks", self.manager.hpc.task_dir(TASK))
            raise RuntimeError("stop before any forcing, SSH or launch")
        self.manager.gfs.select_cycle.side_effect = first_forcing
        with patch("runtime_release.require_ready", return_value={"release_id": "release03", "fingerprint": "fixed-fingerprint"}) as ready:
            with self.assertRaisesRegex(RuntimeError, "stop before"):
                self.manager._execute(TASK)
        self.assertTrue(ready.call_args.kwargs["force"])
        self.assertEqual(ready.call_args.kwargs["request"]["domains"], request()["domains"])

    def test_release_pin_preserves_existing_attempt_and_rejects_drift(self):
        self.manager.hpc = HpcClient(self.config)
        self.store.update(TASK, runtime={"release_id": "release02", "release_fingerprint": "old"})
        with patch("runtime_release.require_ready", return_value={"release_id": "release02", "fingerprint": "old"}) as ready:
            self.manager._pin_runtime_release(TASK, request())
            self.assertEqual(ready.call_args.args[1].hpc_runtime_release, "release02")
        with patch("runtime_release.require_ready", return_value={"release_id": "release02", "fingerprint": "different"}):
            with self.assertRaises(HpcError):
                self.manager._pin_runtime_release(TASK, request())
        self.assertEqual(self.store.get(TASK)["runtime"]["release_fingerprint"], "old")

    def test_legacy_started_attempt_cannot_be_relocated_and_mocks_remain_compatible(self):
        self.manager._pin_runtime_release(TASK, request())
        self.hpc.run.assert_not_called()
        self.manager.hpc = HpcClient(self.config)
        self.store.update(TASK, runtime={"remote_launch_attempted": True})
        with patch("runtime_release.require_ready") as ready:
            with self.assertRaises(HpcError):
                self.manager._pin_runtime_release(TASK, request())
            ready.assert_not_called()

    def test_recovery_rejects_a_worker_that_has_not_finished(self):
        self.store.update(TASK, status="waiting_restart")
        self.manager._task_threads[TASK] = Mock()
        with self.assertRaises(TaskConflictError):
            self.manager.restart(TASK, request(), confirm_task_id=TASK, confirm_attempt=1)
        self.hpc.status.assert_not_called()
        self.hpc.cleanup_task_attempt.assert_not_called()

    def test_legacy_reconcile_timer_prevents_immediate_busy_redispatch(self):
        self.manager._started = True
        with patch("task_manager.threading.Timer", ManualTimer), patch("task_manager.threading.Thread") as spawn:
            self.manager._defer_external(TASK, TimeoutError("connection timed out"))
            self.manager._dispatch_pending()
            spawn.assert_not_called()
            timer = self.manager._reconcile_timers[TASK]
            timer.callback()
            spawn.assert_called_once()

    def test_waiting_restart_with_unknown_attempt_can_be_cancelled(self):
        self.store.update(TASK, status="waiting_restart",
                          runtime={"remote_launch_attempted": True, "launch_attempt": TOKEN})
        with patch.object(self.manager, "_start_remote_cancel") as cancel:
            self.assertEqual(self.manager.cancel(TASK)["status"], "cancel_pending")
            cancel.assert_called_once_with(TASK)

    def test_cancel_read_only_timer_keeps_reservation_then_releases_on_terminal(self):
        self.manager._started = True
        self.manager._dispatch_pending = Mock()
        self.store.update(TASK, status="cancel_pending",
                          runtime={"remote_launch_attempted": True, "launch_attempt": TOKEN})
        self.hpc.status.side_effect = [{"status": "unknown"}, {"status": "failed"}]
        with patch("task_manager.threading.Timer", ManualTimer):
            self.manager._schedule_cancel_reconcile(TASK)
            self.manager._cancel_timers[TASK].callback()
            self.assertEqual(self.store.get(TASK)["status"], "cancel_pending")
            self.store.create("queued", {}, "owner")
            with patch("task_manager.threading.Thread") as spawn:
                self.assertFalse(self.manager._dispatch("queued"))
                spawn.assert_not_called()
            self.manager._cancel_timers[TASK].callback()
        self.assertEqual(self.store.get(TASK)["status"], "cancelled")
        self.assertEqual(self.store.get(TASK)["runtime"]["remote_terminal_status"], "failed")
        self.assertNotIn(TASK, self.manager._cancel_timers)
        self.hpc.cancel.assert_not_called()
        self.hpc.launch.assert_not_called()

    def test_stale_cancel_observation_cannot_overwrite_a_new_attempt(self):
        self.manager._started = True
        self.manager._dispatch_pending = Mock()
        self.store.update(TASK, status="cancel_pending", runtime={"launch_attempt": TOKEN})

        def new_attempt(_task_id):
            self.store.begin_attempt(TASK, request())
            return {"status": "failed"}

        self.hpc.status.side_effect = new_attempt
        with patch("task_manager.threading.Timer", ManualTimer):
            self.manager._schedule_cancel_reconcile(TASK)
            self.manager._cancel_timers[TASK].callback()
        self.assertEqual(self.store.get(TASK)["status"], "queued")
        self.assertEqual(self.store.get(TASK)["attempt_no"], 2)
        self.assertNotIn(TASK, self.manager._cancel_timers)

    def test_transport_unknown_cancel_continues_with_status_reads_only(self):
        self.manager._started = True
        self.manager._dispatch_pending = Mock()
        self.store.update(TASK, status="cancel_pending", runtime={"launch_attempt": TOKEN})
        self.hpc.cancel.side_effect = TimeoutError("response lost")
        self.hpc.status.return_value = {"status": "cancelled"}
        with patch("task_manager.threading.Timer", ManualTimer):
            self.manager._remote_cancel(TASK)
            self.assertEqual(self.store.get(TASK)["status"], "cancel_pending")
            self.manager._cancel_timers[TASK].callback()
        self.assertEqual(self.store.get(TASK)["status"], "cancelled")
        self.hpc.cancel.assert_called_once_with(TASK)
        self.hpc.status.assert_called_once_with(TASK)

    def test_replaced_timer_callback_does_not_read_or_displace_current_timer(self):
        self.manager._started = True
        self.store.update(TASK, status="cancel_pending")
        with patch("task_manager.threading.Timer", ManualTimer):
            self.manager._schedule_cancel_reconcile(TASK)
            old = self.manager._cancel_timers.pop(TASK)
            old.cancel()
            self.manager._schedule_cancel_reconcile(TASK)
            current = self.manager._cancel_timers[TASK]
            old.callback()
        self.assertIs(self.manager._cancel_timers[TASK], current)
        self.hpc.status.assert_not_called()

    def test_stop_cancels_observation_timer(self):
        self.manager._started = True
        self.store.update(TASK, status="cancel_pending")
        with patch("task_manager.threading.Timer", ManualTimer):
            self.manager._schedule_cancel_reconcile(TASK)
            timer = self.manager._cancel_timers[TASK]
            self.manager.stop()
        self.assertTrue(timer.cancelled)
        self.assertFalse(self.manager._cancel_timers)

    def test_duplicate_cancel_does_not_replace_unstarted_registered_thread(self):
        with patch("task_manager.threading.Thread") as spawn:
            spawn.return_value.is_alive.return_value = False
            self.manager._start_remote_cancel(TASK)
            self.manager._start_remote_cancel(TASK)
        spawn.assert_called_once()
        spawn.return_value.start.assert_called_once()


class ManagedLaunchSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.attempt = self.root / "launches" / TOKEN
        self.attempt.mkdir(parents=True)
        managed_process.save(self.attempt / "state.json", {"status": "unknown", "attempt": TOKEN})

    def test_cancellation_between_claim_and_child_prevents_command(self):
        with patch.object(managed_process.subprocess, "Popen") as spawn, patch.object(managed_process.subprocess, "call") as command:
            self.assertEqual(managed_process.cancel(self.root, TOKEN)["status"], "cancel_pending")
            self.assertEqual(managed_process.inspect(self.root, TOKEN)["status"], "unknown")
            managed_process.execute(self.root, TOKEN, "must-not-execute")
            self.assertEqual(managed_process.inspect(self.root, TOKEN)["status"], "cancelled")
            managed_process.execute(self.root, TOKEN, "must-not-execute-again")
            spawn.assert_not_called()
            command.assert_not_called()

    def test_unknown_registered_identity_never_authorizes_signals(self):
        managed_process.save(self.attempt / "state.json", {
            "status": "running", "attempt": TOKEN,
            "process": {"pid": 42, "start_ticks": "old", "boot_id": "old"}})
        with patch.object(managed_process, "process_alive", return_value=False), patch.object(managed_process.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "identity is unknown"):
                managed_process.cancel(self.root, TOKEN)
            spawn.assert_not_called()

    def test_duplicate_execute_does_not_replace_registered_identity(self):
        managed_process.save(self.attempt / "state.json", {"status": "running", "process": {"pid": 42}})
        with patch.object(managed_process.subprocess, "call") as command:
            with self.assertRaisesRegex(RuntimeError, "already registered"):
                managed_process.execute(self.root, TOKEN, "must-not-execute")
            command.assert_not_called()

    def test_launch_handshake_releases_lock_before_child_registration(self):
        root = self.root / "new-control"
        root.mkdir()
        threads = []

        def spawn_child(*args, **kwargs):
            thread = threading.Thread(target=managed_process.execute, args=(root, TOKEN, "mock-command"))
            threads.append(thread)
            thread.start()
            return Mock()

        with patch.object(managed_process, "identity", return_value={"pid": 42, "start_ticks": "now", "boot_id": "mock"}), patch.object(managed_process, "process_alive", side_effect=bool), patch.object(managed_process.subprocess, "Popen", side_effect=spawn_child), patch.object(managed_process.subprocess, "call", return_value=0) as command:
            state = managed_process.launch_claim(root, TOKEN, "mock-command")
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            self.assertIn(state["status"], {"running", "succeeded"})
            command.assert_called_once()
        self.assertEqual(json.loads((root / "launches" / TOKEN / "state.json").read_text())["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
