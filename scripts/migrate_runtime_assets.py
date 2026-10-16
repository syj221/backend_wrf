#!/usr/bin/env python3
"""Offline SQLite backup and exact legacy mappings; check-only by default."""
import argparse
from contextlib import contextmanager, ExitStack, closing
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_assets import SCHEMA, checked_path, digest, task_identity


def readonly_database(path):
    path = Path(path).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def snapshot(path):
    with closing(readonly_database(path)) as connection:
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity check failed")
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        records, configurations, counts = [], [], {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = [dict(row) for row in connection.execute("SELECT * FROM " + quoted)]
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True, default=lambda value: {"blob": value.hex()}))
            counts[name] = len(rows)
            columns = [row[1] for row in connection.execute("PRAGMA table_info(" + quoted + ")")]
            records.append([name, columns, rows])
            if "request_json" in columns:
                configurations.append([name, [[row.get(key) for key in ("id", "task_id", "attempt_no", "owner_sub", "submission_key", "request_json")] for row in rows]])
        # Runtime WRF tables use SQLite primitives; never expose their contents.
        serializable = json.loads(json.dumps(records, default=lambda value: {"blob": value.hex()}))
        return {"table_counts": counts, "records_digest": digest(serializable),
                "configuration_digest": digest(configurations)}


def task_map(path, table, legacy_run_dir):
    mapped = {}
    with closing(readonly_database(path)) as connection:
        present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not present:
            raise ValueError("Source task table is missing")
        for row in connection.execute("SELECT * FROM " + table):
            task = dict(row)
            task_id = task["id"]
            checked_path(legacy_run_dir, task_id + "/service.log")
            mapped[task_id] = {"identity_digest": task_identity(task), "attempt_no": int(task.get("attempt_no") or 1),
                               "record_digest": digest(task), "archived_logs": {}}
        if table == "wrf_tasks" and connection.execute("SELECT 1 FROM sqlite_master WHERE name='wrf_task_attempts'").fetchone():
            root = Path(legacy_run_dir).resolve()
            for row in connection.execute("SELECT task_id,attempt_no,log_path FROM wrf_task_attempts WHERE log_path IS NOT NULL"):
                if row["task_id"] not in mapped:
                    raise ValueError("Archived log has no source task")
                declared = Path(row["log_path"])
                archive_root = checked_path(root, row["task_id"] + "/attempts")
                if not declared.is_absolute() or not declared.is_relative_to(archive_root):
                    raise ValueError("Archived log escaped its exact task root")
                checked_path(archive_root, declared.relative_to(archive_root).as_posix())
                mapped[row["task_id"]]["archived_logs"][str(row["attempt_no"])] = str(declared)
    return mapped


@contextmanager
def reserve_api_ports():
    sockets = []
    try:
        for port in (8007, 8012):
            for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                try:
                    connection = socket.socket(family)
                except OSError as exc:
                    if exc.errno == errno.EAFNOSUPPORT:
                        continue
                    raise
                sockets.append(connection)
                # A gracefully closed API can leave TIME_WAIT connections.
                # Reuse that closed socket state, while an existing listener
                # still makes this exact-address bind fail.
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    connection.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    connection.bind((address, port))
                except OSError as exc:
                    raise RuntimeError("API port {} is occupied; migration refused".format(port)) from exc
        yield
    finally:
        for connection in sockets:
            connection.close()


@contextmanager
def maintenance_guard(workspace_root, held_lock=None):
    path = Path(workspace_root).resolve() / ".run" / "wrf-maintenance.lock"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Existing global maintenance lock is required")
    with ExitStack() as stack:
        lock = held_lock if held_lock is not None else stack.enter_context(path.open("r"))
        actual = os.fstat(lock.fileno())
        expected = path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("Maintenance lock identity changed")
        # Re-locking the same supplied open-file description is nonblocking.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack.enter_context(reserve_api_ports())
        yield


def _write_private(path, value):
    with Path(path).open("x", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())


def migrate_runtime_assets(source_db, target_root, legacy_run_dir, legacy_output_dir, *, workspace_root,
                           apply=False, maintenance_window=False, held_lock=None, legacy_downscale_db=None, audit=None):
    """May be called by maintenance with its existing flock; no main import."""
    if apply and not maintenance_window:
        raise RuntimeError("Explicit exclusive maintenance window is required")
    source_db = Path(source_db).resolve(strict=True)
    target_root = Path(target_root).absolute()
    if target_root.is_symlink() or target_root.exists():
        raise FileExistsError("Migration target must not exist")
    if audit is not None and (Path(audit).exists() or Path(audit).is_symlink()):
        raise FileExistsError("Migration audit target must not exist")
    legacy_run_dir, legacy_output_dir = Path(legacy_run_dir).resolve(), Path(legacy_output_dir).resolve()
    if any(target_root.resolve().is_relative_to(root) for root in (legacy_run_dir, legacy_output_dir, source_db.parent)):
        raise ValueError("New runtime root must be isolated from legacy assets")
    with maintenance_guard(workspace_root, held_lock):
        before = snapshot(source_db)
        tasks = task_map(source_db, "wrf_tasks", legacy_run_dir)
        mappings = [{"kind": "ordinary", "source": str(source_db), "target": str(target_root / "wrf_tasks.sqlite3"), **before}]
        if legacy_downscale_db:
            legacy_downscale_db = Path(legacy_downscale_db).resolve(strict=True)
            downscale_before = snapshot(legacy_downscale_db)
            legacy_tasks = task_map(legacy_downscale_db, "downscale_tasks", legacy_run_dir)
            if set(tasks).intersection(legacy_tasks):
                raise ValueError("Task identities collide across databases")
            tasks.update(legacy_tasks)
            mappings.append({"kind": "downscale", "source": str(legacy_downscale_db), "target": str(legacy_downscale_db), **downscale_before})
        report = {"schema": "wrf-local-assets-migration-1", "status": "checked", "source_database": str(source_db),
                  "target_root": str(target_root), "task_count": len(tasks), "database_mappings": mappings}
        if not apply:
            return report
        target_root.mkdir(mode=0o700, parents=False, exist_ok=False)
        target = target_root / "wrf_tasks.sqlite3"
        try:
            for relative in ("runs", "data", "data/WRF"):
                (target_root / relative).mkdir(mode=0o700)
            with closing(readonly_database(source_db)) as source, closing(sqlite3.connect(target)) as destination:
                os.chmod(target, 0o600)
                source.backup(destination)
            after, copied = snapshot(source_db), snapshot(target)
            if before != after or before != copied:
                raise RuntimeError("Source/backup records or configuration digests changed")
            if legacy_downscale_db and snapshot(legacy_downscale_db) != downscale_before:
                raise RuntimeError("Legacy downscale records changed during backup")
            manifest = {"schema": SCHEMA, "target_database": str(target),
                        "legacy_roots": {"run_dir": str(legacy_run_dir), "output_dir": str(legacy_output_dir)},
                        "database_mappings": mappings, "tasks": tasks}
            _write_private(target_root / "legacy-assets.json", manifest)
            report.update(status="complete", target_database=str(target), backup=copied,
                          legacy_manifest=str(target_root / "legacy-assets.json"), manifest_digest=digest(manifest))
            _write_private(target_root / "migration-audit.json", report)
            if audit is not None:
                _write_private(audit, report)
            return report
        except Exception:
            _write_private(target_root / "migration-failed.json", {**report, "status": "failed",
                           "message": "Partial target retained; inspect before any retry. Source database retained."})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--target-root", "--target", dest="target_root", required=True, type=Path)
    parser.add_argument("--legacy-run-dir", type=Path)
    parser.add_argument("--legacy-output-dir", type=Path)
    parser.add_argument("--legacy-downscale-db", type=Path)
    parser.add_argument("--maintenance-lock-fd", type=int)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--workspace-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maintenance-window", action="store_true")
    values = vars(parser.parse_args())
    if any(values[key] is None for key in ("source_db", "legacy_run_dir", "legacy_output_dir")):
        from config import settings
        values["source_db"] = values["source_db"] or settings.database_path
        values["legacy_run_dir"] = values["legacy_run_dir"] or settings.run_dir
        values["legacy_output_dir"] = values["legacy_output_dir"] or settings.output_dir
        values["legacy_downscale_db"] = values["legacy_downscale_db"] or settings.downscale_database_path
    inherited = values.pop("maintenance_lock_fd")
    with ExitStack() as stack:
        if inherited is not None:
            values["held_lock"] = stack.enter_context(os.fdopen(os.dup(inherited), "r"))
            values["maintenance_window"] = True
        print(json.dumps(migrate_runtime_assets(**values), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
