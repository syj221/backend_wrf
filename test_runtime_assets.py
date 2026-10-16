"""Offline asset migration and exact legacy reads, without SSH or API startup."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from config import settings
from database import TaskStore
from product_snapshot import product_identity, read_frame
from runtime_assets import (RuntimeStaticFiles, archived_log_path, checked_path,
                            log_paths, output_file, read_log_bytes, read_run_path)
from scripts import migrate_runtime_assets as migration
from test_namelist_config import request


TASK = "wrf_gfs_20261008T000000Z_aaaaaaaa"


@contextmanager
def isolated_ports():
    yield


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RuntimeAssetsTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old = self.root / "legacy"
        self.old.mkdir()
        self.runs, self.outputs = self.old / "runs", self.old / "WRF"
        (self.runs / TASK / "raw").mkdir(parents=True)
        (self.runs / TASK / "raw" / "wrfout").write_bytes(b"unchanged native")
        (self.runs / TASK / "service.log").write_bytes(b"old\n")
        self.archive = self.runs / TASK / "attempts" / "001" / "service.log"
        self.archive.parent.mkdir(parents=True)
        self.archive.write_bytes(b"archived\n")
        self.frame = self.outputs / "runs" / TASK / "rendered" / "d01" / "T2" / "frame.webp"
        self.frame.parent.mkdir(parents=True)
        self.frame.write_bytes(b"RIFFfixtureWEBP")
        self.source = self.old / "tasks.sqlite3"
        self.store = TaskStore(self.source)
        self.addCleanup(self.store.close)
        self.store.create(TASK, request(), "alice")
        self.store.archive_attempt(TASK, str(self.archive))
        self.store.begin_attempt(TASK, request())
        result = {"attempt_no": 2, "meta_json": {"created_at": "2026-10-08", "domains": [{
            "id": "d01", "variables": [{"name": "T2", "frames": [{
                "url": f"/data/WRF/runs/{TASK}/rendered/d01/T2/frame.webp", "sha256": sha(self.frame)}]}]}]}}
        self.store.update(TASK, status="succeeded", result=result)
        self.workspace = self.root / "workspace"
        (self.workspace / ".run").mkdir(parents=True)
        self.lockpath = self.workspace / ".run" / "wrf-maintenance.lock"
        self.lockpath.touch()
        self.target = self.workspace / ".run" / "wrf-workbench"
        self.port_patch = patch.object(migration, "reserve_api_ports", isolated_ports)
        self.port_patch.start()
        self.addCleanup(self.port_patch.stop)

    def migrate(self, **kwargs):
        return migration.migrate_runtime_assets(self.source, self.target, self.runs, self.outputs,
            workspace_root=self.workspace, **kwargs)

    def activated(self):
        self.migrate(apply=True, maintenance_window=True)
        return replace(settings, runtime_asset_root=self.target, legacy_asset_manifest=self.target / "legacy-assets.json",
                       database_path=self.target / "wrf_tasks.sqlite3", run_dir=self.target / "runs",
                       data_dir=self.target / "data", downscale_database_path=self.source)

    def test_default_check_is_readonly_and_does_not_create_target(self):
        before = sha(self.source)
        report = self.migrate()
        self.assertEqual(report["status"], "checked")
        self.assertFalse(self.target.exists())
        self.assertEqual(sha(self.source), before)
        with self.assertRaisesRegex(RuntimeError, "maintenance window"):
            self.migrate(apply=True)

    def test_backup_records_configuration_and_permissions_match(self):
        before = sha(self.source)
        report = self.migrate(apply=True, maintenance_window=True)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(migration.snapshot(self.source), migration.snapshot(Path(report["target_database"])))
        self.assertEqual(sha(self.source), before)
        for path in [self.target, self.target / "runs", self.target / "data", self.target / "data/WRF"]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for path in self.target.iterdir():
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        manifest = json.loads((self.target / "legacy-assets.json").read_text())
        self.assertEqual(manifest["tasks"][TASK]["archived_logs"]["1"], str(self.archive))
        self.assertEqual(manifest["database_mappings"][0]["source"], str(self.source))

    def test_inherited_lock_is_reused_but_wrong_or_busy_lock_refuses(self):
        with self.lockpath.open("r") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.migrate(held_lock=held)["status"], "checked")
            with self.assertRaises(BlockingIOError):
                self.migrate()
        with self.source.open("r") as wrong:
            with self.assertRaisesRegex(RuntimeError, "identity"):
                self.migrate(held_lock=wrong)
        self.assertFalse(self.target.exists())

    def test_listener_or_existing_destination_refuses_without_overwrite(self):
        @contextmanager
        def occupied():
            raise RuntimeError("API port 8007 is occupied")
            yield
        with patch.object(migration, "reserve_api_ports", occupied):
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                self.migrate(apply=True, maintenance_window=True)
        self.assertFalse(self.target.exists())
        self.target.mkdir()
        sentinel = self.target / "keep"
        sentinel.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.migrate(apply=True, maintenance_window=True)
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_digest_mismatch_retains_partial_target_and_source(self):
        before = migration.snapshot(self.source)
        source_hash = sha(self.source)
        with patch.object(migration, "snapshot", side_effect=[before, {**before, "records_digest": "changed"}, before]):
            with self.assertRaisesRegex(RuntimeError, "digests changed"):
                self.migrate(apply=True, maintenance_window=True)
        self.assertTrue((self.target / "migration-failed.json").is_file())
        self.assertFalse((self.target / "legacy-assets.json").exists())
        self.assertEqual(sha(self.source), source_hash)

    def test_raw_frame_and_concatenated_logs_read_old_exact_files(self):
        cfg = self.activated()
        task = self.store.get(TASK)
        original = {path: sha(path) for path in (self.frame, self.archive, self.runs / TASK / "service.log")}
        self.assertEqual(read_run_path(cfg, task, "raw"), self.runs / TASK / "raw")
        self.assertEqual(output_file(cfg, task, "rendered/d01/T2/frame.webp"), self.frame)
        (cfg.run_dir / TASK).mkdir()
        (cfg.run_dir / TASK / "service.log").write_bytes(b"new\n")
        self.assertEqual(read_log_bytes(log_paths(cfg, task)), (8, b"old\nnew\n"))
        self.assertEqual(read_log_bytes(log_paths(cfg, task), after=3, limit=3), (6, b"\nne"))
        self.assertEqual(archived_log_path(cfg, task, 1, str(self.archive)), self.archive)
        identity = product_identity(task)
        self.assertEqual(read_frame(task, task["result"], cfg.output_dir,
            f"/data/WRF/runs/{TASK}/rendered/d01/T2/frame.webp", **identity,
            lookup=lambda _id: task, settings=cfg), self.frame.read_bytes())
        self.assertEqual(original, {path: sha(path) for path in original})

    def test_unmapped_identity_new_attempt_and_arbitrary_archives_cannot_fallback(self):
        cfg = self.activated()
        task = self.store.get(TASK)
        for changed in ({**task, "owner_sub": "bob"}, {**task, "attempt_no": 3}, {**task, "id": "unmapped"}):
            with self.assertRaises(FileNotFoundError):
                output_file(cfg, changed, "rendered/d01/T2/frame.webp")
            self.assertEqual(log_paths(cfg, changed), [])
        with self.assertRaises(ValueError):
            archived_log_path(cfg, task, 1, str(self.source))

    def test_symlink_and_traversal_never_extend_legacy_access(self):
        cfg = self.activated()
        task = self.store.get(TASK)
        link = self.frame.parent / "link.webp"
        link.symlink_to(self.frame)
        for relative in ("rendered/d01/T2/link.webp", "../tasks.sqlite3", "/etc/passwd"):
            with self.assertRaises(FileNotFoundError):
                output_file(cfg, task, relative)
        with self.assertRaises(FileNotFoundError):
            checked_path(self.runs, TASK + "/../service.log")

    def test_static_fallback_is_exact_and_does_not_enable_follow_symlink(self):
        cfg = self.activated()
        task = self.store.get(TASK)
        static = RuntimeStaticFiles(cfg, lambda task_id: task if task_id == TASK else None)
        scope = {"type": "http", "method": "GET", "headers": []}
        response = asyncio.run(static.get_response(f"runs/{TASK}/rendered/d01/T2/frame.webp", scope))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Path(response.path), self.frame)
        self.assertFalse(static.follow_symlink)
        from starlette.exceptions import HTTPException
        with self.assertRaises(HTTPException) as failure:
            asyncio.run(static.get_response("runs/unmapped/frame.webp", scope))
        self.assertEqual(failure.exception.status_code, 404)

    def test_downscale_database_is_only_mapped_and_never_copied(self):
        ds = self.old / "downscale.sqlite3"
        with sqlite3.connect(ds) as connection:
            connection.execute("CREATE TABLE downscale_tasks(id TEXT,owner_sub TEXT,created_at TEXT,request_json TEXT)")
            connection.execute("INSERT INTO downscale_tasks VALUES (?,?,?,?)", ("downscale_one", "alice", "2026", "{}"))
        before = sha(ds)
        report = self.migrate(apply=True, maintenance_window=True, legacy_downscale_db=ds)
        mapping = report["database_mappings"][1]
        self.assertEqual((mapping["kind"], mapping["source"], mapping["target"]), ("downscale", str(ds), str(ds)))
        self.assertEqual(sha(ds), before)
        self.assertFalse((self.target / "downscale.sqlite3").exists())

    def test_manifest_for_other_database_is_rejected(self):
        cfg = self.activated()
        other = replace(cfg, database_path=self.root / "other.sqlite3")
        with self.assertRaises(FileNotFoundError):
            output_file(other, self.store.get(TASK), "rendered/d01/T2/frame.webp")


if __name__ == "__main__":
    unittest.main()
