from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_STATES = {
    "queued", "prefetching", "uploading", "running", "rendering", "cancel_pending", "reconciling"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TaskStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS wrf_tasks (
                    id TEXT PRIMARY KEY,
                    owner_sub TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error TEXT,
                    failure_json TEXT,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(wrf_tasks)").fetchall()}
            if "owner_sub" not in columns:
                self._db.execute("ALTER TABLE wrf_tasks ADD COLUMN owner_sub TEXT NOT NULL DEFAULT ''")
            if "failure_json" not in columns:
                self._db.execute("ALTER TABLE wrf_tasks ADD COLUMN failure_json TEXT")
            if "attempt_no" not in columns:
                self._db.execute("ALTER TABLE wrf_tasks ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS wrf_task_attempts (
                    task_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    runtime_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    failure_json TEXT,
                    error TEXT,
                    log_path TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, attempt_no),
                    FOREIGN KEY (task_id) REFERENCES wrf_tasks(id) ON DELETE CASCADE
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        value["runtime"] = json.loads(value.pop("runtime_json") or "{}")
        result = value.pop("result_json")
        value["result"] = json.loads(result) if result else None
        failure = value.pop("failure_json", None)
        value["failure"] = json.loads(failure) if failure else None
        return value

    @staticmethod
    def _decode_attempt(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("request_json", "runtime_json", "result_json", "failure_json"):
            raw = value.pop(key, None)
            value[key.removesuffix("_json")] = json.loads(raw) if raw else None
        return value

    def create(self, task_id: str, request: dict[str, Any], owner_sub: str = "") -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO wrf_tasks (id,owner_sub,status,stage,progress,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, owner_sub, "queued", "queued", 0, json.dumps(request, ensure_ascii=False), now, now),
            )
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM wrf_tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row)

    def list(self, limit: int = 50, owner_sub: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            if owner_sub is None:
                rows = self._db.execute("SELECT * FROM wrf_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM wrf_tasks WHERE owner_sub=? ORDER BY created_at DESC LIMIT ?",
                    (owner_sub, limit),
                ).fetchall()
        return [self._decode(row) for row in rows]

    def active(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM wrf_tasks WHERE status IN ({placeholders}) ORDER BY created_at", tuple(ACTIVE_STATES)
            ).fetchall()
        return [self._decode(row) for row in rows]

    def update(self, task_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"status", "stage", "progress", "request", "runtime", "result", "failure", "error", "attempt_no"}
        updates: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            column = {
                "request": "request_json",
                "runtime": "runtime_json",
                "result": "result_json",
                "failure": "failure_json",
            }.get(key, key)
            if key in {"request", "runtime", "result", "failure"}:
                value = json.dumps(value, ensure_ascii=False) if value is not None else None
            updates.append(f"{column}=?")
            params.append(value)
        updates.append("updated_at=?")
        params.append(utc_now())
        params.append(task_id)
        with self._lock, self._db:
            self._db.execute(f"UPDATE wrf_tasks SET {', '.join(updates)} WHERE id=?", params)
        return self.get(task_id)

    def archive_attempt(self, task_id: str, log_path: str | None = None) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT OR REPLACE INTO wrf_task_attempts (
                    task_id,attempt_no,request_json,status,stage,progress,runtime_json,
                    result_json,failure_json,error,log_path,started_at,finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    int(task.get("attempt_no") or 1),
                    json.dumps(task["request"], ensure_ascii=False),
                    task["status"],
                    task["stage"],
                    int(task.get("progress") or 0),
                    json.dumps(task.get("runtime") or {}, ensure_ascii=False),
                    json.dumps(task.get("result"), ensure_ascii=False) if task.get("result") is not None else None,
                    json.dumps(task.get("failure"), ensure_ascii=False) if task.get("failure") is not None else None,
                    task.get("error"),
                    log_path,
                    (task.get("runtime") or {}).get("attempt_started_at") or task.get("created_at") or now,
                    now,
                ),
            )
        return task

    def begin_attempt(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return self.update(
            task_id,
            attempt_no=int(task.get("attempt_no") or 1) + 1,
            request=request,
            status="queued",
            stage="queued",
            progress=0,
            runtime={"attempt_started_at": utc_now()},
            result=None,
            failure=None,
            error=None,
        )

    def attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM wrf_task_attempts WHERE task_id=? ORDER BY attempt_no DESC",
                (task_id,),
            ).fetchall()
        return [self._decode_attempt(row) for row in rows]

    def with_status(self, *statuses: str) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM wrf_tasks WHERE status IN ({placeholders}) ORDER BY updated_at",
                tuple(statuses),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def latest_success(self, owner_sub: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            query = (
                "SELECT * FROM wrf_tasks WHERE status IN ('succeeded','partial_success') "
                "AND result_json IS NOT NULL"
            )
            params: tuple[Any, ...] = ()
            if owner_sub is not None:
                query += " AND owner_sub=?"
                params = (owner_sub,)
            row = self._db.execute(query + " ORDER BY updated_at DESC LIMIT 1", params).fetchone()
        return self._decode(row)

    def delete(self, task_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM wrf_tasks WHERE id=?", (task_id,))
        return cursor.rowcount > 0
