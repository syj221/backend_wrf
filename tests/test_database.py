from __future__ import annotations

import uuid
import sqlite3
from pathlib import Path

from database import TaskStore


def test_task_store_migrates_existing_database_without_losing_tasks() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    path = root / "tasks.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            """
            CREATE TABLE wrf_tasks (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0, request_json TEXT NOT NULL,
                runtime_json TEXT NOT NULL DEFAULT '{}', result_json TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO wrf_tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("wrf_20260716T000000Z_deadbeef", "failed", "failed", 68, "{}", "{}", None, "old", "now", "now"),
        )
    connection.close()

    store = TaskStore(path)
    try:
        task = store.get("wrf_20260716T000000Z_deadbeef")
        assert task["error"] == "old"
        assert task["attempt_no"] == 1
        assert task["failure"] is None
        assert store.attempts(task["id"]) == []
    finally:
        store.close()


def test_task_store_persists_runtime_state() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    store = TaskStore(root / "tasks.sqlite3")
    task_id = "wrf_20260716T000000Z_deadbeef"
    try:
        store.create(task_id, {"start_time": "2026-07-16T00:00:00Z"})
        store.update(task_id, status="running", runtime={"gfs_cycle": "2026071600"})
        task = store.get(task_id)
        assert task["status"] == "running"
        assert task["runtime"]["gfs_cycle"] == "2026071600"
    finally:
        store.close()


def test_task_store_delete_removes_only_requested_task() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    store = TaskStore(root / "tasks.sqlite3")
    try:
        store.create("wrf_20260716T000000Z_deadbeef", {})
        store.create("wrf_20260716T010000Z_cafebabe", {})
        assert store.delete("wrf_20260716T000000Z_deadbeef") is True
        assert store.get("wrf_20260716T000000Z_deadbeef") is None
        assert store.get("wrf_20260716T010000Z_cafebabe") is not None
    finally:
        store.close()


def test_task_store_archives_attempt_and_reuses_task_id() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    store = TaskStore(root / "tasks.sqlite3")
    task_id = "wrf_gfs_20260722T000000Z_deadbeef"
    try:
        store.create(task_id, {"physics": {"preset": "old"}})
        store.update(
            task_id,
            status="waiting_restart",
            stage="failed",
            progress=68,
            runtime={"gfs_cycle": "2026072100"},
            failure={"failure_class": "model", "recommended_action": "edit_and_restart"},
            error="real.exe failed",
        )
        store.archive_attempt(task_id, str(root / "attempts/001/service.log"))
        restarted = store.begin_attempt(task_id, {"physics": {"preset": "new"}})

        assert restarted["id"] == task_id
        assert restarted["attempt_no"] == 2
        assert restarted["request"]["physics"]["preset"] == "new"
        assert restarted["status"] == "queued"
        assert restarted["failure"] is None
        attempts = store.attempts(task_id)
        assert len(attempts) == 1
        assert attempts[0]["attempt_no"] == 1
        assert attempts[0]["failure"]["failure_class"] == "model"
    finally:
        store.close()
