"""Pure access-policy and recommendation persistence regressions."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from database import TaskStore
from recommendations import RecommendationManager
from static_access import allow_run_asset


TASK_ID = "wrf_gfs_20260924T000000Z_1234abcd"


class WrfAccessTest(unittest.TestCase):
    def test_submission_reconciliation_is_owner_bound_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.sqlite3")
            try:
                reservation = store.reserve_submission("alice", "submission-test", {"source": "gfs"}, TASK_ID)
                self.assertEqual(reservation["accepted"], 0)
                self.assertIsNone(store.get_submission("bob", "submission-test"))
                self.assertEqual(store.get_submission("alice", "submission-test")["task_id"], TASK_ID)
                self.assertEqual(store.get_submission("alice", "submission-test")["accepted"], 0)
            finally:
                store.close()

    def test_unpublished_run_asset_requires_owner(self):
        path = f"/data/WRF/runs/{TASK_ID}/rendered/d01/t2/frame.webp"
        lookup = lambda _task_id: {"owner_sub": "alice"}
        self.assertTrue(allow_run_asset(path, {"sub": "alice", "role": 1}, lookup))
        self.assertFalse(allow_run_asset(path, {"sub": "bob", "role": 1}, lookup))
        self.assertTrue(allow_run_asset(path, {"sub": "admin", "role": 3}, lookup))
        self.assertFalse(allow_run_asset(path.replace("rendered", "../rendered"),
                                         {"sub": "alice", "role": 1}, lookup))
        self.assertTrue(allow_run_asset(
            "/data/WRF/runs/ds_20260924T000000Z_1234abcd/downscale.product.json",
            {"sub": "alice", "role": 1}, lookup,
        ))

    def test_recommendation_job_survives_restart_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(data_dir=Path(directory))
            manager = RecommendationManager(settings, None)
            # Persist a job without starting an external analysis.
            job = {"id": "rec_1234567890abcdef", "owner_sub": "alice",
                   "status": "analyzing_geography", "result": None}
            manager._persist(job)
            manager.stop()
            restored = RecommendationManager(settings, None)
            self.assertIsNone(restored.get(job["id"], "bob", 1))
            self.assertEqual(restored.get(job["id"], "alice", 1)["status"], "interrupted")
            self.assertNotIn("owner_sub", restored.get(job["id"], "alice", 1))
            restored.stop()


if __name__ == "__main__":
    unittest.main()
