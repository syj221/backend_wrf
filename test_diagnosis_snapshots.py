"""Diagnosis observations are durable, scoped and separate from incident claims."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from database import TaskStore


class DiagnosisSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(Path(":memory:"))
        self.addCleanup(self.store.close)
        self.snapshot = {"schema_version": "wrf-diagnosis-1", "task_id": "fixture", "attempt_no": 1,
                         "event_id": "event", "code": "unknown", "actions": [],
                         "blockers": ["More evidence needed"], "diagnosis_digest": "digest",
                         "observed_at": "2026-10-08T00:00:00Z", "evidence_sources": []}

    def test_repeat_observation_deduplicates_without_rewriting_first_snapshot(self):
        first = self.store.save_diagnosis(self.snapshot)
        later = {**self.snapshot, "observed_at": "2026-10-08T01:00:00Z",
                 "evidence_sources": [{"source": "local_log", "status": "available"}]}
        self.assertEqual(first, self.store.save_diagnosis(later))
        self.assertEqual(self.store.diagnoses("fixture", 1), [self.snapshot])
        self.assertEqual(self.store.incidents("fixture"), [])

    def test_changed_evidence_or_recovery_conditions_are_independent_snapshots(self):
        first = self.store.save_diagnosis(self.snapshot)
        changed = {**self.snapshot, "actions": ["resume"], "blockers": [], "remote_status": "unknown"}
        self.assertNotEqual(first, self.store.save_diagnosis(changed))
        self.assertEqual(len(self.store.diagnoses("fixture", 1)), 2)
        self.assertEqual(self.store.diagnoses("other-user-task", 1), [])
        self.assertEqual(self.store.diagnoses("fixture", 2), [])

    def test_raw_evidence_and_execution_request_cannot_enter_snapshot(self):
        value = deepcopy(self.snapshot)
        value.update(evidence="Bearer secret /private/run", request={"secret": "private"}, paths=["/private/run"])
        self.store.save_diagnosis(value)
        saved = self.store.diagnoses("fixture", 1)[0]
        self.assertEqual(saved, self.snapshot)

    def test_reopening_store_retains_snapshot_without_runtime_assets(self):
        with TemporaryDirectory(prefix="wrf-diagnosis-") as folder:
            path = Path(folder) / "fixture.sqlite3"
            store = TaskStore(path)
            store.save_diagnosis(self.snapshot)
            store.close()
            reopened = TaskStore(path)
            try:
                self.assertEqual(reopened.diagnoses("fixture", 1), [self.snapshot])
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
