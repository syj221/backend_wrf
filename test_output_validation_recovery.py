"""Regression tests for explicit output-only validator upgrades."""
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from output_validation_recovery import (apply_plan, canonical, digest, load_bound_task,
    make_plan, task_binding, validate_bound_outputs, verify_override, write_private_json)

TASK_ID = "wrf_ecmwf_20261006T060000Z_3134eb5a"


@dataclass
class Settings:
    database_path: Path
    run_dir: Path
    port: int = 8007


class OutputValidationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="wrf-output-validation-upgrade-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root / "tasks.sqlite3", root / "runs")
        self.config = {"task_id": TASK_ID, "namelist_plan": {"digest": "1" * 64},
                       "time_range": {"spinup_hours": 6}}
        self.config_path = self.settings.run_dir / TASK_ID / "task.config.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps(self.config))
        self.request = {"start_time": "2026-10-06T06:00:00Z", "namelist": {"untouched": True}}
        self.runtime = {"release_id": "ordinary-04", "release_fingerprint": "2" * 64,
                        "launch_attempt": "3" * 32, "remote_wrf_succeeded": True,
                        "remote_terminal_status": "succeeded",
                        "remote_process": {"pid": 123, "start_ticks": "456", "boot_id": "boot"}}
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("CREATE TABLE wrf_tasks(id TEXT PRIMARY KEY,attempt_no INTEGER,status TEXT,request_json TEXT,"
                       "runtime_json TEXT,failure_json TEXT,error TEXT,updated_at TEXT)")
            db.execute("INSERT INTO wrf_tasks VALUES(?,?,?,?,?,?,?,?)", (TASK_ID, 1, "failed",
                       json.dumps(self.request), json.dumps(self.runtime), json.dumps({"failure_class": "output"}),
                       "empty_output_schedule:d01", "unchanged-time"))
        self.observed = {"state": {"status": "succeeded", "attempt": "3" * 32, "exit_code": 0,
                                  "process": self.runtime["remote_process"]}, "config_task_id": TASK_ID,
                         "namelist_plan_digest": "1" * 64,
                         "remote_task_config_sha256": hashlib.sha256(self.config_path.read_bytes()).hexdigest(),
                         "remote_namelist_sha256": "4" * 64, "validation_release_id": "ordinary-05",
                         "validation_release_fingerprint": "5" * 64, "validation_script_sha256": "6" * 64,
                         "validation_schedule_sha256": "7" * 64,
                         "output_inventory": [{"name": "wrfout_d01_2026-10-06_06:00:00", "bytes": 9, "sha256": "8" * 64}],
                         "report": {"schema": "wrf-output-validation-1", "complete": True, "valid": [{"name": "native"}],
                                    "invalid": [], "missing_times": {}}}
        self.observer = lambda *args, **kwargs: deepcopy(self.observed)
        self.planner = lambda *args, **kwargs: make_plan(*args, **kwargs, observer=self.observer)

    def task(self):
        return load_bound_task(self.settings.database_path, TASK_ID, config_path=self.config_path)

    def plan(self):
        return self.planner(self.settings, None, TASK_ID, "ordinary-05")

    def apply(self, plan=None, **kwargs):
        plan = plan or self.plan()
        return apply_plan(plan, plan["digest"], self.settings, None, Path(self.temp.name) / "audit.json",
                          planner=self.planner, guard=lambda settings, **kwargs: [], port_reservation=lambda port: nullcontext(),
                          lock_path=Path(self.temp.name) / "maintenance.lock", **kwargs)

    def test_readonly_plan_does_not_change_database_or_configuration(self):
        before = self.settings.database_path.read_bytes(), self.config_path.read_bytes()
        plan = self.plan()
        self.assertEqual(plan["eligible_action"], "bind_validator")
        self.assertEqual(plan["task_binding"]["namelist_plan_digest"], self.config["namelist_plan"]["digest"])
        self.assertEqual(before, (self.settings.database_path.read_bytes(), self.config_path.read_bytes()))
        self.assertNotIn("output_validation_override", self.task()["runtime"])

    def test_apply_changes_only_runtime_override_and_retains_complete_private_audit(self):
        before = self.task()
        result = self.apply()
        after = self.task()
        self.assertEqual(result["status"], "bound")
        expected = dict(after["runtime"])
        override = expected.pop("output_validation_override")
        self.assertEqual(expected, before["runtime"])
        for key in ("status", "request", "config", "attempt_no", "error", "failure"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(verify_override(after, override), override)
        audit = Path(result["audit_path"])
        self.assertEqual(audit.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(audit.read_text())["phase"], "bound")
        with sqlite3.connect(self.settings.database_path) as db:
            self.assertEqual(db.execute("SELECT updated_at FROM wrf_tasks").fetchone()[0], "unchanged-time")
        self.assertFalse(result["recovery_started"])
        with self.assertRaisesRegex(ValueError, "override already exists"):
            self.plan()

    def test_override_rejects_every_binding_and_digest_drift(self):
        self.apply()
        task = self.task()
        override = task["runtime"]["output_validation_override"]
        for key in (*task_binding(task), "remote_task_config_sha256", "remote_namelist_sha256",
                    "output_inventory_digest", "validation_release_fingerprint", "validation_script_sha256",
                    "validation_schedule_sha256", "audit_id", "bound_at", "schema"):
            changed = dict(override)
            changed[key] = "invalid"
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_override(task, changed)
        changed = deepcopy(task)
        changed["config"]["namelist_plan"]["digest"] = "9" * 64
        with self.assertRaisesRegex(ValueError, "namelist_plan_digest"):
            verify_override(changed, override)

    def test_bound_revalidation_checks_remote_bytes_and_attempt_without_mutating(self):
        self.apply()
        task = self.task()
        before = self.settings.database_path.read_bytes()
        report = validate_bound_outputs(None, self.settings, task, observer=self.observer)
        self.assertTrue(report["complete"])
        self.assertEqual(before, self.settings.database_path.read_bytes())
        for field in ("remote_task_config_sha256", "remote_namelist_sha256", "validation_script_sha256",
                      "validation_schedule_sha256", "validation_release_fingerprint", "namelist_plan_digest"):
            changed = deepcopy(self.observed)
            changed[field] = "9" * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_bound_outputs(None, self.settings, task, observer=lambda *a, **k: changed)
        changed = deepcopy(self.observed)
        changed["output_inventory"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            validate_bound_outputs(None, self.settings, task, observer=lambda *a, **k: changed)

    def test_successful_short_run_is_audit_only_and_cannot_be_bound(self):
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("UPDATE wrf_tasks SET status='succeeded',failure_json=NULL")
        plan = self.plan()
        self.assertEqual(plan["eligible_action"], "readonly_complete_revalidation")
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertEqual(self.task()["status"], "succeeded")

    def test_wrong_or_unknown_remote_attempt_and_output_failure_class_rejected(self):
        for field, value in (("status", "running"), ("attempt", "0" * 32), ("exit_code", 1), ("process", {})):
            original = deepcopy(self.observed)
            self.observed["state"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Remote terminal"):
                self.plan()
            self.observed = original
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("UPDATE wrf_tasks SET failure_json=?", (json.dumps({"failure_class": "data"}),))
        with self.assertRaisesRegex(ValueError, "output-only"):
            self.plan()

    def test_incomplete_quality_evidence_cannot_be_applied(self):
        self.observed["report"].update(complete=False, missing_times={"d01": ["missing"]})
        plan = self.plan()
        self.assertEqual(plan["blockers"], ["complete_output_revalidation_failed"])
        with self.assertRaises(ValueError):
            self.apply(plan)

    def test_drift_after_confirmation_and_bad_confirmation_refuse_without_audit(self):
        plan = self.plan()
        self.observed["output_inventory"][0]["sha256"] = "9" * 64
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.apply(plan)
        self.assertFalse((Path(self.temp.name) / "audit.json").exists())
        with self.assertRaises(ValueError):
            apply_plan(plan, "0" * 64, self.settings, None, Path(self.temp.name) / "audit.json")

    def test_missing_and_symlinked_saved_configuration_refuse_without_regeneration(self):
        self.config_path.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.task()
        external = Path(self.temp.name) / "external.json"
        external.write_text(json.dumps(self.config))
        self.config_path.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.task()

    def test_exclusive_audit_writer_never_overwrites_existing_data(self):
        path = Path(self.temp.name) / "private.json"
        write_private_json(path, {"keep": True})
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            write_private_json(path, {"keep": False})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(digest({"汉字": 1}), hashlib.sha256(canonical({"汉字": 1})).hexdigest())

    def test_agent_task_identity_is_supported_without_widening_task_names(self):
        from output_validation_recovery import TASK
        self.assertTrue(TASK.fullmatch("wrf_agent_" + "a" * 32))
        self.assertTrue(TASK.fullmatch(TASK_ID))
        self.assertFalse(TASK.fullmatch("wrf_agent_../outside"))
        self.assertFalse(TASK.fullmatch("ds_20261006T060000Z_3134eb5a"))

    def test_offline_guard_requires_all_tasks_terminal_or_fresh_waiting_proof(self):
        from output_validation_recovery import offline_guard
        offline_guard(self.settings, port_reserved=True)
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("UPDATE wrf_tasks SET status='running'")
        with self.assertRaisesRegex(RuntimeError, "Every ordinary task"):
            offline_guard(self.settings, port_reserved=True)
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("UPDATE wrf_tasks SET status='waiting_restart'")
        with self.assertRaisesRegex(RuntimeError, "fresh read-only"):
            offline_guard(self.settings, port_reserved=True)
        expected = {"task_id": TASK_ID, "terminal": True}
        proofs = offline_guard(self.settings, port_reserved=True, waiting_verifier=lambda *args: expected)
        self.assertEqual(proofs, [expected])

    def test_port_reservation_covers_both_families_and_all_bind_errors_fail_closed(self):
        import errno
        import socket
        from output_validation_recovery import reserve_api_port
        with patch("output_validation_recovery.socket.socket") as factory:
            connection = factory.return_value
            with reserve_api_port(8007):
                self.assertEqual(factory.call_count, 2)
                connection.bind.assert_any_call(("127.0.0.1", 8007))
                connection.bind.assert_any_call(("::1", 8007))
                connection.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                connection.setsockopt.assert_any_call(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                self.assertEqual(connection.listen.call_count, 2)
                connection.listen.assert_called_with(1)
            for error in (errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL):
                connection.bind.side_effect = OSError(error, "unsafe bind")
                with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, "exit gently"):
                    with reserve_api_port(8007):
                        self.fail("occupied family was accepted")

    def test_waiting_restart_never_allows_unknown_launch_markers(self):
        from output_validation_recovery import verify_waiting_restart
        class Client:
            def task_dir(self, task_id): return "/safe/" + task_id + "/control"
            def run(self, command, timeout):
                return json.dumps({"mode": "not_launched", "terminal": True, "control_absent": True, "attempts_absent": True})
        task = {"id": TASK_ID, "runtime": {"release_id": "ordinary-03"},
                "failure": {"stage": "checking_hpc_forcing"}}
        self.settings.hpc_runtime_python = "/fixed/python"
        self.assertTrue(verify_waiting_restart(self.settings, Client(), task)["terminal"])
        for key, value in (("remote_launch_attempted", "unknown"), ("remote_pid", 12),
                           ("remote_launch_status", "reconciling"), ("launch_attempt", "bad")):
            changed = deepcopy(task)
            changed["runtime"][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                verify_waiting_restart(self.settings, Client(), changed)

    def test_maintenance_lock_contention_and_existing_audit_refuse_before_database_mutation(self):
        import fcntl
        lock_path = Path(self.temp.name) / "maintenance.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.apply()
        audit = Path(self.temp.name) / "audit.json"
        audit.write_text("preserve-existing-audit")
        with self.assertRaises(FileExistsError):
            self.apply()
        self.assertEqual(audit.read_text(), "preserve-existing-audit")
        self.assertNotIn("output_validation_override", self.task()["runtime"])

    def test_terminal_label_does_not_bypass_an_unknown_remote_launch(self):
        from output_validation_recovery import offline_guard
        runtime = deepcopy(self.runtime)
        runtime.update(remote_launch_attempted=True, remote_wrf_succeeded=False, remote_terminal_status=None)
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("UPDATE wrf_tasks SET runtime_json=?", (json.dumps(runtime),))
        with self.assertRaisesRegex(RuntimeError, "unknown remote launch"):
            offline_guard(self.settings, port_reserved=True)

    def test_real_port_reservation_rejects_another_reuseaddr_bind(self):
        import socket
        from output_validation_recovery import reserve_api_port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as finder:
            finder.bind(("127.0.0.1", 0))
            port = finder.getsockname()[1]
        with reserve_api_port(port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as contender:
                contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError):
                    contender.bind(("127.0.0.1", port))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
            released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            released.bind(("127.0.0.1", port))

    def test_inherited_lock_reuses_description_and_preserves_outer_transaction(self):
        import fcntl
        lock_path = Path(self.temp.name) / "maintenance.lock"
        with lock_path.open("a") as outer:
            fcntl.flock(outer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.apply(held_lock=outer)
            self.assertEqual(result["status"], "bound")
            self.assertFalse(outer.closed)
            with lock_path.open("r") as independent:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with lock_path.open("r") as independent:
            fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_wrong_inherited_lock_inode_and_symlink_are_rejected(self):
        from output_validation_recovery import held_maintenance_lock
        path = Path(self.temp.name) / "maintenance.lock"
        path.write_text("")
        wrong = Path(self.temp.name) / "wrong.lock"
        with wrong.open("a") as handle:
            with self.assertRaisesRegex(RuntimeError, "exact global lock inode"):
                with held_maintenance_lock(path, handle):
                    self.fail("wrong descriptor was accepted")
        link = Path(self.temp.name) / "link.lock"
        link.symlink_to(path)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            with held_maintenance_lock(link):
                self.fail("symlink lock was accepted")
