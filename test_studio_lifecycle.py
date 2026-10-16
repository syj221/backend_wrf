import ast
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("managed_process", ROOT / "scripts/managed_process.py")
process = importlib.util.module_from_spec(spec)
spec.loader.exec_module(process)


class StudioLifecycleTests(unittest.TestCase):
    def test_all_legacy_recovery_routes_reject_confirm_before_execution(self):
        names = {"get_legacy_recovery_task", "retry_task", "restart_task", "resume_task", "retry_task_outputs"}
        nodes = [node for node in ast.parse((ROOT / "main.py").read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        manager = Mock(hpc_health={"status": "ready"})
        visible = Mock(return_value={"request": {"recovery_policy": "confirm"}})
        namespace = dict(Request=object, Any=object, WrfTaskRestartRequest=object,
                         HTTPException=HTTPException, get_visible_task=visible,
                         task_manager=manager, ok=lambda value, **kw: value,
                         TaskNotFoundError=KeyError, TaskConflictError=RuntimeError, HpcError=OSError)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
        for name in names - {"get_legacy_recovery_task"}:
            args = ("task", object(), object()) if name == "restart_task" else ("task", object())
            with self.subTest(route=name), self.assertRaises(HTTPException) as caught:
                namespace[name](*args)
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(manager.mock_calls, [])
        visible.return_value = {"request": {}}
        namespace['retry_task']("task", object())
        manager.retry.assert_called_once_with("task")

    def test_same_attempt_and_unknown_attempt_never_spawn_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(process.subprocess, "Popen") as spawn:
                first = process.launch(root, "a" * 32, "true")
                second = process.launch(root, "a" * 32, "true")
                self.assertEqual(first["status"], "unknown")
                self.assertEqual(second["status"], "unknown")
                spawn.assert_called_once()
                with self.assertRaisesRegex(RuntimeError, "replay refused"):
                    process.launch(root, "b" * 32, "true")
                spawn.assert_called_once()
                process.save(root / "launches" / ("a" * 32) / "state.json", {"status": "failed"})
                process.launch(root, "b" * 32, "true")
                self.assertEqual(spawn.call_count, 2)

    def test_pid_reuse_is_unknown_and_legacy_pid_cannot_authorize_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "launches" / ("a" * 32)
            path.mkdir(parents=True)
            process.save(path / "state.json", {"status": "running", "process": {"pid": 42, "start_ticks": "old"}})
            with patch.object(process, "identity", return_value={"pid": 42, "start_ticks": "new"}):
                self.assertEqual(process.inspect(root, "a" * 32)["status"], "unknown")
            (root / "service.pid").write_text("42")
            with patch.object(process.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(RuntimeError, "Legacy"):
                    process.launch(root, "b" * 32, "true")
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
