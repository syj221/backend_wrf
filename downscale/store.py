from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DownscaleStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS downscale_tasks (
                    id TEXT PRIMARY KEY, owner_sub TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, progress INTEGER NOT NULL, request_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL DEFAULT '{}', result_json TEXT,
                    quality_json TEXT NOT NULL DEFAULT '{}', publication_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS downscale_submissions (
                    owner_sub TEXT NOT NULL, submission_key TEXT NOT NULL,
                    request_json TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(owner_sub, submission_key)
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS downscale_confirmations (
                    token_digest TEXT PRIMARY KEY, owner_sub TEXT NOT NULL,
                    request_json TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS downscale_logs (
                    task_id TEXT NOT NULL, seq INTEGER NOT NULL, created_at TEXT NOT NULL,
                    message TEXT NOT NULL, PRIMARY KEY(task_id, seq),
                    FOREIGN KEY(task_id) REFERENCES downscale_tasks(id) ON DELETE CASCADE
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS mesoscale_forcing_artifacts (
                    artifact_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL, owner_sub TEXT NOT NULL, created_at TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS downscale_terrain_submissions (
                    id TEXT PRIMARY KEY, owner_sub TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, review_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS downscale_certification_requests (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, owner_sub TEXT NOT NULL,
                    recipe_hash TEXT NOT NULL, evidence_json TEXT NOT NULL, status TEXT NOT NULL,
                    review_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES downscale_tasks(id) ON DELETE CASCADE
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS downscale_certification_profiles (
                    id TEXT PRIMARY KEY, recipe_hash TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                )
            """)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(downscale_tasks)")}
            if "certification_json" not in columns:
                self._db.execute("ALTER TABLE downscale_tasks ADD COLUMN certification_json TEXT NOT NULL DEFAULT '{}' ")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for source, target in (
            ("request_json", "request"), ("runtime_json", "runtime"),
            ("result_json", "result"), ("quality_json", "quality"),
            ("publication_json", "publication"), ("certification_json", "certification"),
        ):
            raw = value.pop(source, None)
            value[target] = json.loads(raw) if raw else None
        value["task_type"] = "high_resolution_downscale"
        return value

    def save_confirmation(self, digest: str, owner_sub: str, request: dict[str, Any], expires_at: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO downscale_confirmations VALUES (?,?,?,?,NULL)",
                (digest, owner_sub, json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":")), expires_at),
            )

    def consume_confirmation(self, digest: str, owner_sub: str, canonical_request: str, now: str) -> bool:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM downscale_confirmations WHERE token_digest=?", (digest,)).fetchone()
            if not row or row["owner_sub"] != owner_sub or row["consumed_at"] or row["expires_at"] <= now:
                return False
            if row["request_json"] != canonical_request:
                return False
            self._db.execute("UPDATE downscale_confirmations SET consumed_at=? WHERE token_digest=?", (now, digest))
        return True

    def reserve_submission(self, owner_sub: str, key: str, request: dict[str, Any], task_id: str) -> str:
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO downscale_submissions VALUES (?,?,?,?)", (owner_sub, key, canonical, task_id))
            row = self._db.execute("SELECT * FROM downscale_submissions WHERE owner_sub=? AND submission_key=?", (owner_sub, key)).fetchone()
            if row["request_json"] != canonical:
                raise ValueError("同一幂等键不能提交不同高分辨率任务")
            return str(row["task_id"])

    def submission(self, owner_sub: str, key: str, canonical_request: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM downscale_submissions WHERE owner_sub=? AND submission_key=?", (owner_sub, key)
            ).fetchone()
        if not row:
            return None
        if row["request_json"] != canonical_request:
            raise ValueError("同一幂等键不能提交不同高分辨率任务")
        return str(row["task_id"])

    def create(
        self,
        task_id: str,
        owner_sub: str,
        request: dict[str, Any],
        runtime: dict[str, Any],
        *,
        status: str = "queued",
        stage: str = "waiting_upstream",
        certification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO downscale_tasks
                (id, owner_sub, status, stage, progress, request_json, runtime_json, result_json,
                 quality_json, publication_json, error, created_at, updated_at, certification_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, owner_sub, status, stage, 0,
                 json.dumps(request, ensure_ascii=False), json.dumps(runtime, ensure_ascii=False), None,
                 json.dumps({"status": "pending", "gates": []}, ensure_ascii=False),
                 json.dumps({"status": "blocked"}, ensure_ascii=False), None, now, now,
                 json.dumps(certification or {"status": "research"}, ensure_ascii=False)),
            )
        self.append_log(task_id, "任务已受理；等待受管 WRF/CFD 运行器执行。" if status == "queued" else "任务已受理；等待资源与科学审核。")
        return self.get(task_id) or {}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._decode(self._db.execute("SELECT * FROM downscale_tasks WHERE id=?", (task_id,)).fetchone())

    def list(self, limit: int, owner_sub: str | None) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            if owner_sub is None:
                rows = self._db.execute("SELECT * FROM downscale_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM downscale_tasks WHERE owner_sub=? ORDER BY created_at DESC LIMIT ?", (owner_sub, limit)).fetchall()
        return [self._decode(row) for row in rows if row]

    def update(self, task_id: str, **values: Any) -> dict[str, Any] | None:
        allowed = {"status", "stage", "progress", "runtime", "result", "quality", "publication", "certification", "error"}
        columns: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            column = {"runtime": "runtime_json", "result": "result_json", "quality": "quality_json", "publication": "publication_json", "certification": "certification_json"}.get(key, key)
            if key in {"runtime", "result", "quality", "publication", "certification"}:
                value = json.dumps(value, ensure_ascii=False) if value is not None else None
            columns.append(f"{column}=?")
            params.append(value)
        columns.append("updated_at=?")
        params.extend([utc_now(), task_id])
        with self._lock, self._db:
            self._db.execute(f"UPDATE downscale_tasks SET {', '.join(columns)} WHERE id=?", params)
        return self.get(task_id)

    def append_log(self, task_id: str, message: str) -> None:
        with self._lock, self._db:
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM downscale_logs WHERE task_id=?", (task_id,)).fetchone()
            self._db.execute("INSERT INTO downscale_logs VALUES (?,?,?,?)", (task_id, int(row["seq"]) + 1, utc_now(), message))

    def logs(self, task_id: str, after: int, limit: int) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM downscale_logs WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, max(0, after), max(1, min(limit, 65536)))).fetchall()
        text = "".join(f"[{row['created_at']}] {row['message']}\n" for row in rows)
        return {"text": text, "next_offset": int(rows[-1]["seq"]) if rows else after}

    def register_forcing(self, artifact_id: str, content_sha256: str, manifest: dict[str, Any], owner_sub: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO mesoscale_forcing_artifacts VALUES (?,?,?,?,?)", (artifact_id, content_sha256, json.dumps(manifest, ensure_ascii=False), owner_sub, utc_now()))

    def forcing(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM mesoscale_forcing_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["manifest"] = json.loads(value.pop("manifest_json"))
        return value

    def create_terrain_submission(self, submission_id: str, owner_sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO downscale_terrain_submissions VALUES (?,?,?,?,?,?,?)",
                (submission_id, owner_sub, json.dumps(payload, ensure_ascii=False), "pending_review", None, now, now),
            )
        return self.terrain_submission(submission_id) or {}

    def terrain_submission(self, submission_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM downscale_terrain_submissions WHERE id=?", (submission_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        value["review"] = json.loads(value.pop("review_json")) if value.get("review_json") else None
        return value

    def update_terrain_submission(self, submission_id: str, status: str, review: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE downscale_terrain_submissions SET status=?, review_json=?, updated_at=? WHERE id=?",
                (status, json.dumps(review, ensure_ascii=False), utc_now(), submission_id),
            )
        return self.terrain_submission(submission_id)

    def create_certification_request(
        self, request_id: str, task_id: str, owner_sub: str, recipe_hash: str, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO downscale_certification_requests VALUES (?,?,?,?,?,?,?,?,?)",
                (request_id, task_id, owner_sub, recipe_hash, json.dumps(evidence, ensure_ascii=False), "pending_review", None, now, now),
            )
        return self.certification_request(request_id) or {}

    def certification_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM downscale_certification_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json"))
        value["review"] = json.loads(value.pop("review_json")) if value.get("review_json") else None
        return value

    def update_certification_request(self, request_id: str, status: str, review: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE downscale_certification_requests SET status=?, review_json=?, updated_at=? WHERE id=?",
                (status, json.dumps(review, ensure_ascii=False), utc_now(), request_id),
            )
        return self.certification_request(request_id)

    def register_certification_profile(self, profile_id: str, recipe_hash: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO downscale_certification_profiles VALUES (?,?,?,?,?)",
                (profile_id, recipe_hash, task_id, json.dumps(payload, ensure_ascii=False), utc_now()),
            )
        return self.certification_profile(profile_id) or {}

    def certification_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM downscale_certification_profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def certification_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM downscale_certification_profiles ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            result.append(value)
        return result
