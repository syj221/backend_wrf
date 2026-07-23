from __future__ import annotations

import uuid
import threading
import time
from dataclasses import replace
from pathlib import Path

from config import settings
from database import TaskStore
from hpc_transport import HpcError, SAFE_TASK_ID
from recommendations import RecommendationManager
from task_manager import TaskConflictError, WrfTaskManager
import pytest


class FakeHpc:
    def health(self):
        return {"status": "ready", "message": "test"}

    def status(self, _task_id):
        return {"status": "running", "log": "WRF is running"}

    def close_session(self):
        pass


def test_new_task_id_includes_gfs_data_source() -> None:
    task_id = WrfTaskManager.new_task_id()

    assert task_id.startswith("wrf_gfs_")
    assert SAFE_TASK_ID.fullmatch(task_id)


def test_retry_outputs_requeues_same_task_without_new_wrf_run() -> None:
    task_id = "wrf_gfs_20260718T000000Z_deadbeef"
    task = {
        "id": task_id,
        "status": "failed",
        "stage": "failed",
        "progress": 88,
        "error": "超算输出分块下载解码失败",
        "runtime": {
            "remote_pid": 123,
            "gfs_cycle": "2026071700",
            "remote_wrf_succeeded": True,
        },
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    manager = WrfTaskManager(settings, MemoryStore(), None, FakeHpc())
    manager._log = lambda *_args: None

    result = manager.retry_outputs(task_id)

    assert result["id"] == task_id
    assert result["status"] == "queued"
    assert result["stage"] == "retrying_outputs"
    assert result["runtime"]["retry_outputs_only"] is True
    assert manager._queue.get_nowait() == task_id


def test_retry_outputs_rejects_remote_wrf_failure() -> None:
    task_id = "wrf_gfs_20260718T000000Z_deadbeef"
    task = {
        "id": task_id,
        "status": "failed",
        "runtime": {"remote_pid": 123, "gfs_cycle": "2026071700"},
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

    manager = WrfTaskManager(settings, MemoryStore(), None, FakeHpc())
    with pytest.raises(TaskConflictError, match="远端 WRF 尚未成功"):
        manager.retry_outputs(task_id)


def test_execute_retry_outputs_only_skips_wrf_and_gfs(monkeypatch) -> None:
    task_id = "wrf_gfs_20260718T000000Z_cafebabe"
    task = {
        "id": task_id,
        "status": "queued",
        "stage": "retrying_outputs",
        "progress": 88,
        "error": None,
        "request": {},
        "runtime": {
            "remote_pid": 123,
            "gfs_cycle": "2026071700",
            "retry_outputs_only": True,
        },
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    class CompletedHpc(FakeHpc):
        def status(self, _task_id):
            return {"status": "succeeded"}

    finalized = []
    manager = WrfTaskManager(settings, MemoryStore(), None, CompletedHpc())
    monkeypatch.setattr(manager, "_log", lambda *_args: None)
    monkeypatch.setattr(manager, "_finalize", lambda task_id, cycle: finalized.append((task_id, cycle)))

    manager._execute(task_id)

    assert finalized == [(task_id, "2026071700")]
    assert task["runtime"]["retry_outputs_only"] is False
    assert task["runtime"]["remote_wrf_succeeded"] is True


def test_cancel_before_remote_launch_is_immediate(monkeypatch) -> None:
    task_id = "wrf_gfs_20260717T000000Z_deadbeef"
    task = {
        "id": task_id,
        "status": "uploading",
        "stage": "preparing_hpc",
        "progress": 38,
        "runtime": {},
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    class NoRemoteCancelHpc(FakeHpc):
        def cancel(self, _task_id):
            raise AssertionError("远端进程尚未启动时不应等待堡垒机会话")

    manager = WrfTaskManager(settings, MemoryStore(), None, NoRemoteCancelHpc())
    monkeypatch.setattr(manager, "_log", lambda *_args: None)

    result = manager.cancel(task_id)

    assert result["status"] == "cancelled"
    assert result["stage"] == "cancelled"


def test_remote_cancel_runs_in_background(monkeypatch) -> None:
    task_id = "wrf_gfs_20260717T000000Z_cafebabe"
    task = {
        "id": task_id,
        "status": "running",
        "stage": "running",
        "progress": 68,
        "runtime": {"remote_pid": 12345},
    }
    entered = threading.Event()
    release = threading.Event()

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    class BlockingCancelHpc(FakeHpc):
        def cancel(self, _task_id):
            entered.set()
            release.wait(2)
            return {"status": "term_sent"}

    manager = WrfTaskManager(settings, MemoryStore(), None, BlockingCancelHpc())
    monkeypatch.setattr(manager, "_log", lambda *_args: None)

    started = time.monotonic()
    result = manager.cancel(task_id)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert result["status"] == "cancel_pending"
    assert entered.wait(1)
    release.set()
    deadline = time.monotonic() + 2
    while task["status"] != "cancelled" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert task["status"] == "cancelled"


def test_start_finishes_prelaunch_cancel_pending_without_requeue(monkeypatch) -> None:
    cancelled_id = "wrf_gfs_20260717T000000Z_deadbeef"
    queued_id = "wrf_gfs_20260717T000001Z_cafebabe"
    tasks = {
        cancelled_id: {
            "id": cancelled_id,
            "status": "cancel_pending",
            "stage": "cancel_pending",
            "progress": 38,
            "runtime": {},
        },
        queued_id: {
            "id": queued_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "runtime": {},
        },
    }

    class QueueStore:
        def active(self):
            return list(tasks.values())

        def get(self, task_id):
            return tasks[task_id]

        def update(self, task_id, **values):
            tasks[task_id].update(values)
            return tasks[task_id]

    cfg = replace(settings, max_concurrent_tasks=1)
    manager = WrfTaskManager(cfg, QueueStore(), None, FakeHpc())
    executed = threading.Event()
    seen = []
    monkeypatch.setattr(manager, "_log", lambda *_args: None)
    monkeypatch.setattr(
        manager,
        "_execute",
        lambda task_id: seen.append(task_id) or executed.set(),
    )

    manager.start()
    try:
        assert executed.wait(1)
    finally:
        manager.stop()

    assert tasks[cancelled_id]["status"] == "cancelled"
    assert seen == [queued_id]


def test_execute_skips_local_gfs_when_remote_manifest_is_complete(monkeypatch) -> None:
    task_id = "wrf_20260716T000000Z_deadbeef"
    task = {
        "id": task_id,
        "status": "queued",
        "runtime": {},
        "request": {
            "start_time": "2026-07-16T00:00:00Z",
            "end_time": "2026-07-16T06:00:00Z",
            "forecast_interval_hours": 6,
        },
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    class NoDownloadGfs:
        def select_cycle(self, *_args):
            return "2026071600", [0, 12]

        def required_hours(self, *_args):
            return [0, 6, 12]

        def ensure_cycle(self, *_args, **_kwargs):
            raise AssertionError("远端缓存完整时不应下载本地 GFS")

    class CompleteRemoteHpc(FakeHpc):
        prepared = False
        transfer_status = {
            "mode": "pending",
            "state": "idle",
            "message": "等待传输运行文件",
        }

        def inspect_gfs_files(self, _cycle, hours):
            return {
                "complete": True,
                "valid_hours": hours,
                "missing_hours": [],
                "entries": [],
            }

        def prepare_runtime(self, *_args, **_kwargs):
            self.prepared = True
            self.transfer_status = {
                "mode": "pty_fallback",
                "state": "succeeded",
                "message": "原生 SFTP 不可用，PTY 回退传输成功",
            }

        def ensure_gfs_files(self, *_args, **_kwargs):
            raise AssertionError("远端缓存完整时不应上传 GFS")

        def launch(self, _task_id):
            return {
                "remote_pid": 123,
                "remote_task_dir": "~/task",
                "remote_output_dir": "~/output",
            }

    hpc = CompleteRemoteHpc()
    manager = WrfTaskManager(settings, MemoryStore(), NoDownloadGfs(), hpc)
    monkeypatch.setattr(manager, "_log", lambda *_args: None)
    monkeypatch.setattr(manager, "_monitor", lambda *_args: False)
    monkeypatch.setattr("task_manager.write_task_bundle", lambda *_args: None)

    manager._execute(task_id)

    assert hpc.prepared is True
    assert task["runtime"]["gfs_remote_reused"] == 3
    assert task["runtime"]["hpc_transfer"] == {
        "mode": "pty_fallback",
        "state": "succeeded",
        "message": "原生 SFTP 不可用，PTY 回退传输成功",
    }


def test_execute_never_falls_back_to_local_gfs_upload(monkeypatch) -> None:
    task_id = "wrf_gfs_20260717T000000Z_deadbeef"
    task = {
        "id": task_id,
        "status": "queued",
        "runtime": {},
        "request": {
            "start_time": "2026-07-16T00:00:00Z",
            "end_time": "2026-07-16T06:00:00Z",
            "forecast_interval_hours": 6,
        },
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    class FullDownloadGfs:
        downloaded = []

        def select_cycle(self, *_args):
            return "2026071600", [0, 12]

        def required_hours(self, *_args):
            return [0, 6, 12]

        def ensure_cycle(self, cycle, hours, *_args, **_kwargs):
            self.downloaded = list(hours)
            return [Path(f"/tmp/gfs.t00z.pgrb2.0p25.f{hour:03d}") for hour in hours]

    class IncompatibleRemoteHpc(FakeHpc):
        uploaded = []

        def inspect_gfs_files(self, _cycle, hours):
            return {
                "complete": False,
                "valid_hours": [],
                "missing_hours": hours,
                "entries": [],
                "remote_dir": "~/Data/gfsdata/2026071600",
                "legacy_imported_hours": [],
                "manifest_needs_rebuild": False,
                "manifest_is_full": False,
            }

        def ensure_gfs_files(self, _cycle, files, required, **kwargs):
            assert required == [0, 6, 12]
            assert kwargs["existing_entries"] == []
            self.uploaded = [path.name for path in files]

        def prepare_runtime(self, *_args, **_kwargs):
            pass

        def launch(self, _task_id):
            return {
                "remote_pid": 123,
                "remote_task_dir": "~/task",
                "remote_output_dir": "~/output",
            }

    gfs = FullDownloadGfs()
    hpc = IncompatibleRemoteHpc()
    manager = WrfTaskManager(settings, MemoryStore(), gfs, hpc)
    monkeypatch.setattr(manager, "_log", lambda *_args: None)
    monkeypatch.setattr(manager, "_monitor", lambda *_args: False)
    monkeypatch.setattr("task_manager.write_task_bundle", lambda *_args: None)

    with pytest.raises(RuntimeError, match="不支持远端共享下载"):
        manager._execute(task_id)

    assert gfs.downloaded == []
    assert hpc.uploaded == []


def test_three_workers_run_and_fourth_task_waits(monkeypatch) -> None:
    task_ids = [f"wrf_20260716T00000{index}Z_deadbee{index}" for index in range(4)]
    tasks = {
        task_id: {"id": task_id, "status": "queued", "progress": 0, "runtime": {}}
        for task_id in task_ids
    }

    class QueueStore:
        def active(self):
            return list(tasks.values())

        def get(self, task_id):
            return tasks.get(task_id)

        def update(self, task_id, **values):
            tasks[task_id].update(values)
            return tasks[task_id]

    class ParallelHpc(FakeHpc):
        closed = False

        def close_session(self):
            self.closed = True

    cfg = replace(settings, max_concurrent_tasks=3)
    hpc = ParallelHpc()
    manager = WrfTaskManager(cfg, QueueStore(), None, hpc)
    release = threading.Event()
    first_three_started = threading.Event()
    fourth_started = threading.Event()
    started = []
    started_lock = threading.Lock()

    def execute(task_id):
        with started_lock:
            started.append(task_id)
            if len(started) == 3:
                first_three_started.set()
            if len(started) == 4:
                fourth_started.set()
        release.wait(2)

    monkeypatch.setattr(manager, "_execute", execute)
    manager.start()
    try:
        assert first_three_started.wait(2)
        assert manager.active_task_count == 3
        assert len(started) == 3
        release.set()
        assert fourth_started.wait(2)
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while manager.active_task_count and time.monotonic() < deadline:
            time.sleep(0.01)
        manager.stop()

    assert len(started) == 4
    assert hpc.closed is True


def test_reconcile_reattaches_to_running_remote_task() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    cfg = replace(settings, run_dir=root / "runs", database_path=root / "tasks.sqlite3")
    store = TaskStore(cfg.database_path)
    task_id = "wrf_20260716T000000Z_deadbeef"
    try:
        store.create(task_id, {"start_time": "2026-07-16T00:00:00Z"})
        store.update(task_id, status="reconciling", stage="reconciling")
        manager = WrfTaskManager(cfg, store, None, FakeHpc())
        assert manager._reconcile(task_id) == "monitor"
        assert store.get(task_id)["status"] == "running"
    finally:
        store.close()


def test_monitor_does_not_finalize_when_service_is_stopping() -> None:
    manager = WrfTaskManager(settings, None, None, FakeHpc())
    manager._stop.set()

    assert manager._monitor("wrf_20260716T000000Z_deadbeef") is False


def test_external_connection_error_enters_reconciliation_without_failing(monkeypatch) -> None:
    task_id = "wrf_gfs_20260722T000000Z_deadbeef"
    task = {
        "id": task_id,
        "status": "running",
        "stage": "running",
        "progress": 68,
        "runtime": {"remote_pid": 123, "gfs_cycle": "2026072100"},
        "attempt_no": 1,
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    manager = WrfTaskManager(settings, MemoryStore(), None, FakeHpc())
    scheduled = []
    monkeypatch.setattr(manager, "_log", lambda *_args: None)
    monkeypatch.setattr(manager, "_schedule_reconcile", lambda value: scheduled.append(value))

    manager._defer_external(task_id, HpcError("超算会话重建后仍未进入计算节点 Shell"))

    assert task["status"] == "reconciling"
    assert task["progress"] == 68
    assert task["failure"]["failure_class"] == "external"
    assert task["failure"]["recommended_action"] == "resume"
    assert scheduled == [task_id]


def test_legacy_session_failure_is_exposed_as_resumable_external_failure() -> None:
    task = WrfTaskManager._decorate_legacy_failure({
        "id": "wrf_gfs_20260722T000000Z_deadbeef",
        "status": "failed",
        "stage": "failed",
        "error": "超算会话重建后仍未进入计算节点 Shell",
        "runtime": {"remote_pid": 123},
    })

    assert task["failure"]["failure_class"] == "external"
    assert task["failure"]["recommended_action"] == "resume"


def test_external_connection_error_pauses_after_timeout(monkeypatch) -> None:
    task_id = "wrf_gfs_20260722T000000Z_cafebabe"
    task = {
        "id": task_id,
        "status": "reconciling",
        "stage": "reconciling",
        "progress": 68,
        "runtime": {"external_retry_started_at": "2020-01-01T00:00:00Z"},
        "attempt_no": 1,
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

        def update(self, _task_id, **values):
            task.update(values)
            return task

    manager = WrfTaskManager(settings, MemoryStore(), None, FakeHpc())
    monkeypatch.setattr(manager, "_log", lambda *_args: None)
    monkeypatch.setattr(manager, "_schedule_reconcile", lambda *_args: pytest.fail("超时后不应再次调度"))

    manager._defer_external(task_id, HpcError("堡垒机连接超时"))

    assert task["status"] == "paused_external"
    assert task["stage"] == "paused_external"


def test_restart_archives_attempt_and_cleans_only_task_paths(monkeypatch) -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    cfg = replace(settings, data_dir=root / "data", run_dir=root / "runs", database_path=root / "tasks.sqlite3")
    store = TaskStore(cfg.database_path)
    task_id = "wrf_gfs_20260722T000000Z_feedface"

    class RestartHpc(FakeHpc):
        def __init__(self):
            self.cleaned = []

        def status(self, _task_id):
            return {"status": "failed", "exit_code": "1", "log": "namelist mismatch"}

        def task_artifact_paths(self, _task_id):
            return [f"/remote/backend_wrf_tasks/{task_id}", f"/remote/WRF_{task_id}"]

        def cleanup_task_attempt(self, _task_id, *, allow_missing=False):
            self.cleaned.append((_task_id, allow_missing))
            return {"status": "failed", "deleted_paths": self.task_artifact_paths(_task_id)}

    try:
        store.create(task_id, {"physics": {"preset": "old"}})
        store.update(
            task_id,
            status="waiting_restart",
            stage="failed",
            progress=68,
            runtime={"remote_pid": 99, "remote_launch_attempted": True},
            failure={"failure_class": "configuration", "recommended_action": "edit_and_restart"},
            error="namelist mismatch",
        )
        run_dir = cfg.run_dir / task_id
        output_dir = cfg.output_dir / "runs" / task_id
        (run_dir / "raw").mkdir(parents=True)
        output_dir.mkdir(parents=True)
        (run_dir / "service.log").write_text("old log", encoding="utf-8")
        (run_dir / "raw" / "partial").write_text("partial", encoding="utf-8")
        (output_dir / "scene.meta.json").write_text("{}", encoding="utf-8")
        hpc = RestartHpc()
        manager = WrfTaskManager(cfg, store, None, hpc)
        monkeypatch.setattr(manager, "_log", lambda *_args: None)

        result = manager.restart(
            task_id,
            {"physics": {"preset": "new"}},
            confirm_task_id=task_id,
            confirm_attempt=1,
        )

        assert result["id"] == task_id
        assert result["attempt_no"] == 2
        assert result["request"]["physics"]["preset"] == "new"
        assert hpc.cleaned == [(task_id, False)]
        assert (run_dir / "attempts/001/service.log").read_text(encoding="utf-8") == "old log"
        assert (run_dir / "raw").is_dir()
        assert not (run_dir / "raw" / "partial").exists()
        assert not output_dir.exists()
        assert store.attempts(task_id)[0]["attempt_no"] == 1
    finally:
        store.close()


def test_restart_plan_refuses_running_remote_task() -> None:
    task_id = "wrf_gfs_20260722T000000Z_1234abcd"
    task = {
        "id": task_id,
        "status": "waiting_restart",
        "stage": "failed",
        "progress": 68,
        "runtime": {"remote_pid": 123, "remote_launch_attempted": True},
        "attempt_no": 1,
        "request": {},
    }

    class MemoryStore:
        def get(self, _task_id):
            return task

    class RunningHpc(FakeHpc):
        def task_artifact_paths(self, _task_id):
            return [f"/remote/WRF_{task_id}"]

    manager = WrfTaskManager(settings, MemoryStore(), None, RunningHpc())
    plan = manager.restart_plan(task_id)

    assert plan["can_restart"] is False
    assert plan["remote_status"] == "running"
    assert "仍在运行" in plan["reason"]


def test_delete_local_removes_final_task_data() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    cfg = replace(settings, data_dir=root / "data", run_dir=root / "runs", database_path=root / "tasks.sqlite3")
    store = TaskStore(cfg.database_path)
    task_id = "wrf_20260716T000000Z_deadbeef"
    try:
        store.create(task_id, {})
        store.update(task_id, status="succeeded", stage="done")
        run_path = cfg.run_dir / task_id
        output_path = cfg.output_dir / "runs" / task_id
        run_path.mkdir(parents=True)
        output_path.mkdir(parents=True)
        (run_path / "service.log").write_text("done", encoding="utf-8")
        (output_path / "scene.meta.json").write_text("{}", encoding="utf-8")
        manager = WrfTaskManager(cfg, store, None, FakeHpc())

        result = manager.delete_local(task_id, task_id)

        assert result["removed"] == {"run_dir": True, "output_dir": True, "task_record": True}
        assert not run_path.exists()
        assert not output_path.exists()
        assert store.get(task_id) is None
    finally:
        store.close()


def test_delete_local_rejects_active_task_and_wrong_confirmation() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    cfg = replace(settings, data_dir=root / "data", run_dir=root / "runs", database_path=root / "tasks.sqlite3")
    store = TaskStore(cfg.database_path)
    task_id = "wrf_20260716T000000Z_deadbeef"
    try:
        store.create(task_id, {})
        manager = WrfTaskManager(cfg, store, None, FakeHpc())
        with pytest.raises(ValueError, match="确认任务 ID"):
            manager.delete_local(task_id, "wrf_wrong")
        with pytest.raises(TaskConflictError, match="运行中任务不能删除"):
            manager.delete_local(task_id, task_id)
    finally:
        store.close()


def test_recommendation_uses_geography_season_and_domain_resolution() -> None:
    request = {
        "center": {"lat": 38.1, "lon": 114.8},
        "start_time": "2026-07-21T00:00:00Z",
        "forecast_focus": "urban",
        "domains": [
            {"id": "d01", "dx": 9000},
            {"id": "d02", "dx": 3000},
        ],
    }
    geography = {
        "domains": [
            {"domain": "d01", "terrain_max_m": 1800, "terrain_range_m": 1500, "terrain_std_m": 380, "land_fraction": 0.7, "water_fraction": 0.3, "urban_fraction": 0.08},
            {"domain": "d02", "terrain_max_m": 900, "terrain_range_m": 700, "terrain_std_m": 180, "land_fraction": 0.8, "water_fraction": 0.2, "urban_fraction": 0.22},
        ]
    }

    result = RecommendationManager._recommend(request, geography)

    assert result["factors"]["season"] == "暖季"
    assert result["factors"]["complex_terrain"] is True
    assert result["factors"]["coastal"] is True
    assert result["physics"]["bl_pbl_physics"] == 2
    assert result["physics"]["cu_physics_by_domain"] == [3, 0]
    assert result["physics"]["sf_urban_physics_by_domain"] == [0, 1]
    assert result["spinup"]["hours"] == 12


def test_recommendation_selects_cold_season_mixed_phase_microphysics() -> None:
    request = {
        "center": {"lat": 45.0, "lon": 126.0},
        "start_time": "2026-01-15T00:00:00Z",
        "forecast_focus": "snowfall",
        "domains": [{"id": "d01", "dx": 15000}],
    }
    geography = {
        "domains": [
            {"domain": "d01", "terrain_max_m": 500, "terrain_range_m": 300, "terrain_std_m": 80, "land_fraction": 0.95, "water_fraction": 0.05, "urban_fraction": 0.03},
        ]
    }

    result = RecommendationManager._recommend(request, geography)

    assert result["factors"]["season"] == "冷季"
    assert result["physics"]["mp_physics"] == 16
    assert result["physics"]["bl_pbl_physics"] == 2
    assert result["assimilation_scheme"] == "fdda_standard"
