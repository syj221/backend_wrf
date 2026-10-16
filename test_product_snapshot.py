"""Ordinary WRF display identity, stale-result and private-asset regressions."""

import ast
import hashlib
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from fastapi import HTTPException, Query, Request
from fastapi.responses import Response

from product_snapshot import ProductSnapshotError, bind_display, product_identity, read_frame


TASK = "wrf_gfs_20261005T090000Z_39ae93ba"


class ProductSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = f"/data/WRF/runs/{TASK}/rendered/d01/T2/frame.webp"
        self.content = b"RIFFfixtureWEBP"
        self.target = self.root / self.path.removeprefix("/data/WRF/")
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.content)
        self.task = {"id": TASK, "status": "succeeded", "attempt_no": 2, "result": {
            "attempt_no": 2, "meta_json": {"created_at": "2026-10-08T00:00:00Z", "domains": [
                {"id": "d01", "variables": [{"name": "T2", "frames": [{
                    "url": self.path, "sha256": hashlib.sha256(self.content).hexdigest()}]}]}]}}}

    def read(self, *, task=None, path=None, lookup=None):
        task = task or self.task
        return read_frame(task, task["result"], self.root, path or self.path,
                          **product_identity(self.task), lookup=lookup or (lambda _id: self.task))

    def test_identity_is_stable_and_same_attempt_rebuild_changes_it(self):
        original = product_identity(self.task)
        self.task["updated_at"] = "later poll"
        self.assertEqual(product_identity(self.task), original)
        self.task["result"]["meta_json"]["created_at"] = "2026-10-08T00:05:00Z"
        self.assertNotEqual(product_identity(self.task), original)

    def test_summary_and_display_share_identity_without_mutating_result(self):
        result = bind_display(self.task, self.task["result"])
        self.assertEqual({key: result[key] for key in product_identity(self.task)}, product_identity(self.task))
        self.assertNotIn("product_version", self.task["result"])

    def test_manifest_only_read_is_bounded_and_checksum_checked(self):
        self.assertEqual(self.read(), self.content)
        with self.assertRaises(FileNotFoundError):
            self.read(path=self.path.replace("frame", "other"))
        self.target.write_bytes(b"different new image")
        with self.assertRaises(ProductSnapshotError):
            self.read()

    def test_new_attempt_cannot_return_old_result(self):
        self.task["attempt_no"] = 3
        self.assertIsNone(product_identity(self.task)["product_version"])
        with self.assertRaises(ProductSnapshotError):
            bind_display(self.task, self.task["result"])

    def test_retry_outputs_or_restart_during_read_is_rejected(self):
        for state in ("rendering", "new_version", "new_attempt"):
            with self.subTest(state=state):
                changed = deepcopy(self.task)
                if state == "rendering":
                    changed["status"] = "rendering"
                elif state == "new_version":
                    changed["result"]["meta_json"]["created_at"] = "new build"
                else:
                    changed["attempt_no"] = 3
                with self.assertRaises(ProductSnapshotError):
                    self.read(lookup=lambda _id: changed)

    def test_outside_task_symlink_cannot_be_read(self):
        outside = self.root / "outside.webp"
        outside.write_bytes(self.content)
        self.target.unlink()
        self.target.symlink_to(outside)
        with self.assertRaises(FileNotFoundError):
            self.read()

    def test_asset_route_checks_owner_before_product_access_and_disables_cache(self):
        tree = ast.parse(Path(__file__).with_name("main.py").read_text())
        route = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "task_frame")
        route.decorator_list = []
        events = []
        def visible(*_args):
            events.append("owner")
            return self.task
        def display(task, settings):
            events.append("display")
            return task["result"]
        namespace = {"Request": Request, "Query": Query, "Response": Response, "HTTPException": HTTPException,
                     "ProductSnapshotError": ProductSnapshotError, "get_visible_task": visible,
                     "corrected_display": display, "read_frame": read_frame,
                     "settings": SimpleNamespace(output_dir=self.root)}
        exec(compile(ast.Module(body=[route], type_ignores=[]), "main.py", "exec"), namespace)
        result = namespace["task_frame"](TASK, object(), self.path, **product_identity(self.task))
        self.assertEqual(events, ["owner", "display", "owner"])
        self.assertEqual(result.body, self.content)
        self.assertEqual(result.headers["cache-control"], "private, no-store")
        def forbidden(*_args):
            raise HTTPException(404)
        namespace["get_visible_task"] = forbidden
        events.clear()
        with self.assertRaises(HTTPException):
            namespace["task_frame"](TASK, object(), self.path, **product_identity(self.task))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
