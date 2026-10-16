import json
import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from legacy_display import corrected_display


class LegacyDisplayTest(unittest.TestCase):
    def test_display_route_recovers_only_after_task_visibility_check(self):
        # Compile only the route to avoid starting service infrastructure in a unit test.
        tree = ast.parse(Path(__file__).with_name("main.py").read_text())
        route = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "display")
        route.decorator_list = []
        task = {"status": "succeeded", "result": {"meta_json": {}}}
        events = []
        def visible(task_id, request):
            events.append("visible")
            return task
        def recover(value, settings):
            events.append("recover")
            self.assertIs(value, task)
            return {"recovered": True}
        namespace = {"Request": object, "Any": object, "_viewer": lambda request: ("owner", 1),
                     "get_visible_task": visible, "corrected_display": recover, "settings": object(), "ok": lambda value: value}
        exec(compile(ast.Module(body=[route], type_ignores=[]), "main.py", "exec"), namespace)
        self.assertEqual(namespace["display"](object(), "task"), {"recovered": True})
        self.assertEqual(events, ["visible", "recover"])

    def test_missing_raw_keeps_original_and_reports_actionable_reason(self):
        with tempfile.TemporaryDirectory() as root:
            task = {"id": "old", "status": "succeeded", "request": {}, "result": {"meta_json": {"domains": [{"id": "d01"}]}}}
            result = corrected_display(task, SimpleNamespace(run_dir=Path(root), output_dir=Path(root)))
            self.assertEqual(result["geolocation_repair"]["status"], "unavailable")
            self.assertNotIn("geolocation_repair", task["result"])

    def test_recovery_is_isolated_cached_and_does_not_relabel_old_images(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            settings = SimpleNamespace(run_dir=root / "raw", output_dir=root / "products")
            raw = settings.run_dir / "old" / "raw"
            raw.mkdir(parents=True)
            (raw / "wrfout_d01_test").write_bytes(b"synthetic")
            task = {"id": "old", "status": "succeeded", "request": {}, "result": {"meta_url": "/original.json", "meta_json": {"domains": [{"id": "d01"}]}}}
            def render(task_id, raw_dir, output_dir, request, cycle, **kwargs):
                self.assertIn("/geolocated/", str(output_dir))
                manifest = {"domains": [{"id": "d01", "geolocation": {"status": "reprojected"}, "variables": [{"frames": [{"url": kwargs["asset_prefix"] + "/rendered/frame.webp"}]}]}]}
                output_dir.mkdir(parents=True)
                (output_dir / "scene.meta.json").write_text(json.dumps(manifest))
                return manifest
            with patch("legacy_display.render_run", side_effect=render) as mocked:
                first = corrected_display(task, settings)
                second = corrected_display(task, settings)
            self.assertEqual(first, second)
            self.assertEqual(mocked.call_count, 1)
            self.assertNotIn("geolocation", task["result"]["meta_json"]["domains"][0])

    def test_invalid_projection_does_not_claim_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            raw = root / "task" / "raw"
            raw.mkdir(parents=True)
            (raw / "wrfout_d01_test").write_bytes(b"synthetic")
            task = {"id": "task", "status": "succeeded", "request": {}, "result": {"meta_json": {"domains": [{"id": "d01"}]}}}
            with patch("legacy_display.render_run", side_effect=ValueError("invalid geometry")):
                result = corrected_display(task, SimpleNamespace(run_dir=root, output_dir=root / "products"))
            self.assertEqual(result["geolocation_repair"]["status"], "failed")
            self.assertNotIn("geolocation", result["meta_json"]["domains"][0])
