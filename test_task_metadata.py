"""Task metadata cannot mutate frozen computation or leak across owners."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from database import TaskStore


class TaskMetadataTest(unittest.TestCase):
    def test_page_revision_owner_and_immutable_request(self):
        with TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.sqlite3")
            try:
                store.create("gfs-one", {"data_source": "gfs", "start_time": "2026-10-09T00:00:00Z"}, "alice")
                store.create("ecmwf-one", {"data_source": "ecmwf"}, "bob")
                before = store.get("gfs-one")
                metadata = store.patch_metadata("gfs-one", 0, {"name": "华东个例", "notes": "第一次试算"})
                self.assertEqual(metadata["version"], 1)
                self.assertEqual(store.get("gfs-one"), before)
                self.assertEqual([row["id"] for row in store.page(10, "alice")["items"]], ["gfs-one"])
                self.assertEqual(store.page(10, "bob", search="华东")["items"], [])
                self.assertEqual(store.page(10, None, source="ecmwf")["total"], 1)
                with self.assertRaisesRegex(ValueError, "其他操作"):
                    store.patch_metadata("gfs-one", 0, {"archived": True})
                store.patch_metadata("gfs-one", 1, {"archived": True})
                self.assertEqual(store.page(10, "alice")["total"], 0)
                self.assertEqual(store.page(10, "alice", archived=True)["total"], 1)
                with self.assertRaisesRegex(ValueError, "不允许"):
                    store.patch_metadata("gfs-one", 2, {"request": {"data_source": "ecmwf"}})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
