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
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS wrf_tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
        return value

    def create(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO wrf_tasks (id,status,stage,progress,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, "queued", "queued", 0, json.dumps(request, ensure_ascii=False), now, now),
            )
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM wrf_tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            rows = self._db.execute("SELECT * FROM wrf_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode(row) for row in rows]

    def active(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM wrf_tasks WHERE status IN ({placeholders}) ORDER BY created_at", tuple(ACTIVE_STATES)
            ).fetchall()
        return [self._decode(row) for row in rows]

    def update(self, task_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"status", "stage", "progress", "runtime", "result", "error"}
        updates: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            column = {"runtime": "runtime_json", "result": "result_json"}.get(key, key)
            if key in {"runtime", "result"}:
                value = json.dumps(value, ensure_ascii=False) if value is not None else None
            updates.append(f"{column}=?")
            params.append(value)
        updates.append("updated_at=?")
        params.append(utc_now())
        params.append(task_id)
        with self._lock, self._db:
            self._db.execute(f"UPDATE wrf_tasks SET {', '.join(updates)} WHERE id=?", params)
        return self.get(task_id)

    def latest_success(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM wrf_tasks WHERE status IN ('succeeded','partial_success') "
                "AND result_json IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return self._decode(row)

    def delete(self, task_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM wrf_tasks WHERE id=?", (task_id,))
        return cursor.rowcount > 0
