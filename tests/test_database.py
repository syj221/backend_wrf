from __future__ import annotations

import uuid
from pathlib import Path

from database import TaskStore


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
