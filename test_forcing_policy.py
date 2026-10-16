"""Policy must reject a second scheduler and protect explicit cycle holds."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from database import TaskStore
from forcing_policy import policy_update, request_download, observe_download, cancel_download
from hpc import HpcError


class ForcingPolicyTest(unittest.TestCase):
    def observation_fixture(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = TaskStore(Path(directory.name) / "wrf.sqlite3")
        self.addCleanup(store.close)
        client = Mock()
        client.trigger_forcing_download.return_value = {"status": "queued"}
        operation = request_download(store, client, "alice", "ecmwf", "2026100900", 0, 6)
        return store, client, operation

    def test_progress_tracks_only_missing_requested_hours_without_resubmission(self):
        store, client, operation = self.observation_fixture()
        client.inspect_forcing_files.return_value = {"complete": False, "valid_hours": [0], "missing_hours": [3,6]}
        client.forcing_download_progress.return_value = {"status": "failed", "failed_hours": [3], "last_error": "下载失败"}
        result = observe_download(store, client, operation)
        self.assertEqual(result["observation"]["status"], "failed")
        client.forcing_download_progress.assert_called_once_with("ecmwf", "2026100900", [3,6])
        self.assertEqual(store.forcing_operation(operation["id"])["status"], "accepted")
        self.assertEqual(client.trigger_forcing_download.call_count, 1)

    def test_task_window_ready_does_not_require_full_cycle(self):
        store, client, operation = self.observation_fixture()
        client.inspect_forcing_files.return_value = {"complete": True, "valid_hours": [0,3,6], "missing_hours": []}
        self.assertEqual(observe_download(store, client, operation)["status"], "ready")
        client.forcing_download_progress.assert_not_called()
        self.assertEqual(store.forcing_operation(operation["id"])["detail"]["hours"], [0,3,6])

    def test_cancel_wins_against_late_ready_observation(self):
        store, client, operation = self.observation_fixture()
        def inspect(*args):
            cancel_download(store, client, operation)
            return {"complete": True, "valid_hours": [0,3,6]}
        client.inspect_forcing_files.side_effect = inspect
        self.assertEqual(observe_download(store, client, operation)["status"], "cancelled")
        self.assertEqual(store.forcing_operation(operation["id"])["status"], "cancelled")

    def test_unavailable_observation_never_replays_unknown_request(self):
        store, client, operation = self.observation_fixture()
        store.update_forcing_operation(operation["id"], "unknown", {})
        client.inspect_forcing_files.side_effect = HpcError("private remote path")
        result = observe_download(store, client, store.forcing_operation(operation["id"]))
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("private", str(result["observation"]))
        self.assertEqual(client.trigger_forcing_download.call_count, 1)

    def test_versioned_policy_rejects_old_cron_and_conflicts(self):
        with TemporaryDirectory() as directory:
            store=TaskStore(Path(directory)/"wrf.sqlite3")
            self.addCleanup(store.close)
            manager=Mock()
            client=SimpleNamespace(run=Mock(return_value="*/10 * * * * wrf_server_cron.sh"))
            settings=SimpleNamespace(hpc_workbench_data_root="/workbench",hpc_gfs_dir="/workbench/forcing/gfs",hpc_ec_dir="/workbench/forcing/ecmwf",hpc_gfs_prefetch_enabled=False)
            holds={"gfs":["2026100900"],"ecmwf":[]}
            with self.assertRaisesRegex(ValueError,"旧 WRF cron"):
                policy_update(store,manager,client,settings,version=0,enabled=True,auto_cleanup=False,held_cycles=holds)
            client.run.return_value="*/15 * * * * unrelated_job"
            saved=policy_update(store,manager,client,settings,version=0,enabled=True,auto_cleanup=True,held_cycles=holds)
            self.assertEqual(saved["version"],1)
            self.assertEqual(saved["held_cycles"],holds)
            manager.set_forcing_policy.assert_called_once()
            with self.assertRaisesRegex(ValueError,"其他操作"):
                policy_update(store,manager,client,settings,version=0,enabled=False,auto_cleanup=False,held_cycles=holds)

    def test_selected_hours_are_persisted_and_uncertain_submit_is_not_replayed(self):
        with TemporaryDirectory() as directory:
            store=TaskStore(Path(directory)/"wrf.sqlite3")
            self.addCleanup(store.close)
            client=SimpleNamespace(trigger_forcing_download=Mock(return_value={"status":"queued"}))
            operation=request_download(store,client,"alice","ecmwf","2026100900",3,9)
            self.assertEqual(operation["detail"],{"hours":[3,6,9],"status":"queued"})
            client.trigger_forcing_download.assert_called_once_with("ecmwf","2026100900",9,[3,6,9],reference=operation["id"])
            client.trigger_forcing_download.side_effect=RuntimeError("transport unknown")
            unknown=request_download(store,client,"alice","gfs","2026100900",0,1)
            self.assertEqual(unknown["status"],"unknown")
            self.assertEqual(unknown["detail"]["hours"],[0,1])
            self.assertEqual(client.trigger_forcing_download.call_count,2)
            rows=store._db.execute("SELECT status FROM wrf_forcing_operations ORDER BY rowid").fetchall()
            self.assertEqual([r[0] for r in rows],["accepted","unknown"])


if __name__=="__main__":unittest.main()
