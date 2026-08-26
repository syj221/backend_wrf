from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import Settings
from database import TaskStore
from gfs import GfsManager
from hpc import HpcClient, HpcError, write_task_bundle
from renderer import render_run
from schemas import FIXED_RUNTIME_PROFILE, FIXED_SPINUP_HOURS, normalize_task_request


FINAL_STATES = {"succeeded", "partial_success", "failed", "cancelled", "waiting_restart"}
EXTERNAL_ERROR_MARKERS = (
    "堡垒", "会话", "连接", "认证", "密码", "shell", "timeout", "timed out",
    "connection", "disconnect", "broken pipe", "eof", "permission denied",
)


def resolve_spinup_hours(request: dict[str, Any]) -> int:
    del request
    return FIXED_SPINUP_HOURS


class TaskNotFoundError(KeyError):
    pass


class TaskConflictError(RuntimeError):
    pass


class TaskCancelledError(RuntimeError):
    pass


class RemoteTaskFailure(RuntimeError):
    def __init__(self, status: dict[str, Any]):
        self.remote_status = status
        exit_code = status.get("exit_code", "?")
        failure = status.get("failure") or {}
        detail = failure.get("command") or failure.get("message") or ""
        super().__init__(f"远端 WRF 退出码 {exit_code}{f'：{detail}' if detail else ''}")


class WrfTaskManager:
    def __init__(self, settings: Settings, store: TaskStore, gfs: GfsManager, hpc: HpcClient):
        self.settings = settings
        self.store = store
        self.gfs = gfs
        self.hpc = hpc
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._started = False
        self._task_threads: dict[str, threading.Thread] = {}
        self._pending_task_ids: set[str] = set()
        self._cancelled: set[str] = set()
        self._cancel_threads: dict[str, threading.Thread] = {}
        self._reconcile_timers: dict[str, threading.Timer] = {}
        self._active_task_ids: set[str] = set()
        self._cycle_locks: dict[str, threading.Lock] = {}
        self._gfs_sync_lock = threading.Lock()
        self._gfs_prefetch_thread: threading.Thread | None = None
        self._gfs_prefetch: dict[str, Any] = {
            "enabled": bool(settings.hpc_gfs_prefetch_enabled and gfs is not None),
            "status": "idle" if settings.hpc_gfs_prefetch_enabled else "disabled",
            "message": "等待后台检查最新 GFS 00Z"
            if settings.hpc_gfs_prefetch_enabled
            else "GFS 后台预取已关闭",
            "interval_seconds": settings.hpc_gfs_prefetch_interval_seconds,
            "updated_at": None,
            "target_cycles": [],
        }
        self._health: dict[str, Any] = {"status": "checking", "message": "等待检查 tx-lab 连接"}

    @property
    def queue_size(self) -> int:
        with self._state_lock:
            return len(self._pending_task_ids)

    @property
    def active_task_id(self) -> str | None:
        values = self.active_task_ids
        return values[0] if values else None

    @property
    def active_task_ids(self) -> list[str]:
        with self._state_lock:
            return sorted(self._active_task_ids)

    @property
    def active_task_count(self) -> int:
        with self._state_lock:
            return len(self._active_task_ids)

    @property
    def hpc_health(self) -> dict[str, Any]:
        return dict(self._health)

    @property
    def gfs_prefetch_status(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._gfs_prefetch)

    def _set_gfs_prefetch_status(self, **values: Any) -> None:
        with self._state_lock:
            self._gfs_prefetch.update(values)
            self._gfs_prefetch["updated_at"] = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )

    def refresh_hpc_health(self) -> dict[str, Any]:
        self._check_health()
        return self.hpc_health

    def authenticate_hpc(self, password: str) -> dict[str, Any]:
        self._health = self.hpc.authenticate_password(password)
        if self._health.get("status") == "ready":
            self._resume_paused_tasks()
        return self.hpc_health

    def _check_health(self) -> None:
        self._health = self.hpc.health()
        if self._health.get("status") == "ready":
            self._resume_paused_tasks()

    def _resume_paused_tasks(self) -> None:
        resumable = getattr(self.store, "with_status", lambda *_args: [])("paused_external")
        for task in resumable:
            try:
                self.resume_external(task["id"], automatic=True)
            except (TaskConflictError, TaskNotFoundError):
                continue

    def sync_latest_gfs(
        self,
        *,
        cleanup_old_cycles: bool = True,
        target_cycles: list[str] | None = None,
    ) -> dict[str, Any]:
        """串行检查并触发最近 00Z；实际下载由 tx-lab 的 nohup 进程持有。"""
        if self.gfs is None:
            raise RuntimeError("GFS 周期管理器未配置")
        with self._gfs_sync_lock:
            selected_cycles = list(target_cycles or self.gfs.latest_cycles())
            protected_cycles = self.protected_gfs_cycles()
            auto_cleanup = {"requested": [], "deleted": [], "missing": []}
            if cleanup_old_cycles:
                auto_cleanup = self.hpc.auto_cleanup_gfs_cycles(
                    selected_cycles,
                    protected_cycles,
                )
            pool = self.hpc.gfs_pool_items(selected_cycles, protected_cycles)[0]
            by_cycle = {item["cycle"]: item for item in pool.get("cycles") or []}
            actions: list[dict[str, Any]] = []
            for cycle in reversed(selected_cycles):
                item = by_cycle.get(cycle) or {}
                if item.get("complete"):
                    actions.append({"cycle": cycle, "status": "ready", "detail": "73/73"})
                else:
                    actions.append(self.hpc.trigger_gfs_download(cycle, 72))
            return {
                "target_cycles": selected_cycles,
                "actions": actions,
                "auto_cleanup": auto_cleanup,
            }

    def _prefetch_latest_gfs_once(self) -> None:
        if not self._gfs_prefetch.get("enabled"):
            return
        if self.hpc_health.get("status") != "ready":
            storage_blocked = self.hpc_health.get("status") == "storage_unavailable"
            self._set_gfs_prefetch_status(
                status="blocked_storage" if storage_blocked else "waiting_connection",
                message=(
                    "tx-lab GFS 存储不可用，已停止后台下载重试"
                    if storage_blocked
                    else "等待 tx-lab 连接恢复后继续后台预取"
                ),
            )
            return
        demand = self.pending_gfs_demand()
        if demand["task_count"]:
            cycles = "、".join(demand["cycles"]) or "周期选择中"
            self._set_gfs_prefetch_status(
                status="paused_for_task",
                message=f"后台预取已让路：{demand['task_count']} 个任务需要 {cycles}",
                task_demand=demand,
            )
            return
        self._set_gfs_prefetch_status(status="checking", message="正在检查最新 GFS 00Z")
        try:
            # 只在新的受管中国区域池中保留配置数量的周期；活动任务周期始终受保护。
            result = self.sync_latest_gfs(
                cleanup_old_cycles=True,
                target_cycles=self.gfs.latest_cycles(
                    count=self.settings.hpc_gfs_retained_cycles,
                ),
            )
        except Exception as exc:
            self._set_gfs_prefetch_status(status="error", message=str(exc))
            return
        actions = result.get("actions") or []
        statuses = {str(item.get("status") or "") for item in actions}
        if actions and statuses == {"ready"}:
            status = "ready"
            message = "最新 GFS 00Z 已就绪"
        elif statuses & {"started", "running"}:
            status = "downloading"
            message = "tx-lab 正在后台下载 GFS；完成后会继续检查新周期"
        elif statuses & {"failed", "error"}:
            status = "error"
            message = "GFS 后台下载启动失败，将自动重试"
        else:
            status = "checked"
            message = "已完成 GFS 后台检查"
        self._set_gfs_prefetch_status(
            status=status,
            message=message,
            target_cycles=result.get("target_cycles") or [],
            actions=actions,
            auto_cleanup=result.get("auto_cleanup") or {},
        )

    def _gfs_prefetch_loop(self) -> None:
        if self._stop.wait(self.settings.hpc_gfs_prefetch_start_delay_seconds):
            return
        while not self._stop.is_set():
            self._prefetch_latest_gfs_once()
            if self._stop.wait(self.settings.hpc_gfs_prefetch_interval_seconds):
                return

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
        threading.Thread(target=self._check_health, daemon=True, name="wrf-hpc-health").start()
        pending_cancellations: list[dict[str, Any]] = []
        for task in self.store.active():
            if task["status"] == "cancel_pending":
                with self._state_lock:
                    self._cancelled.add(task["id"])
                pending_cancellations.append(task)
                continue
            if task["status"] != "queued":
                self.store.update(task["id"], status="reconciling", stage="reconciling")
            self._dispatch(task["id"])
        if self._gfs_prefetch.get("enabled") and not (
            self._gfs_prefetch_thread and self._gfs_prefetch_thread.is_alive()
        ):
            self._gfs_prefetch_thread = threading.Thread(
                target=self._gfs_prefetch_loop,
                daemon=True,
                name="wrf-gfs-prefetch",
            )
            self._gfs_prefetch_thread.start()
        for task in pending_cancellations:
            if (task.get("runtime") or {}).get("remote_pid"):
                self._start_remote_cancel(task["id"])
            else:
                self._log(task["id"], "服务重启后确认任务尚未启动远端进程，完成取消")
                self.store.update(
                    task["id"],
                    status="cancelled",
                    stage="cancelled",
                    error=None,
                )

    def stop(self) -> None:
        self._stop.set()
        with self._state_lock:
            task_threads = list(self._task_threads.values())
            prefetch_thread = self._gfs_prefetch_thread
            cancel_threads = list(self._cancel_threads.values())
            reconcile_timers = list(self._reconcile_timers.values())
            self._reconcile_timers.clear()
        for timer in reconcile_timers:
            timer.cancel()
        deadline = time.monotonic() + 5
        for thread in task_threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if prefetch_thread:
            prefetch_thread.join(timeout=max(0, deadline - time.monotonic()))
        for thread in cancel_threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if (
            not any(thread.is_alive() for thread in task_threads)
            and not (prefetch_thread and prefetch_thread.is_alive())
            and not any(thread.is_alive() for thread in cancel_threads)
        ):
            self.hpc.close_session()
        with self._state_lock:
            self._started = False
            self._pending_task_ids.clear()
            self._task_threads = {
                task_id: thread
                for task_id, thread in self._task_threads.items()
                if thread.is_alive()
            }

    def _dispatch(self, task_id: str) -> bool:
        """为任务创建独立执行线程；同一任务仍在执行时幂等返回。"""
        with self._state_lock:
            if not self._started:
                return False
            existing = self._task_threads.get(task_id)
            if existing:
                return False
            if self._stop.is_set():
                return False
            thread = threading.Thread(
                target=self._run_task,
                args=(task_id,),
                daemon=True,
                name=f"wrf-task-{task_id[-8:]}",
            )
            self._task_threads[task_id] = thread
            self._pending_task_ids.add(task_id)
        thread.start()
        return True

    @staticmethod
    def new_task_id(data_source: str = "gfs") -> str:
        source = str(data_source or "gfs").lower()
        if source not in {"gfs", "ecmwf"}:
            raise ValueError("数据源必须是 gfs 或 ecmwf")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"wrf_{source}_{stamp}_{uuid.uuid4().hex[:8]}"

    def submit(self, request: dict[str, Any], owner_sub: str = "") -> dict[str, Any]:
        request = normalize_task_request(request)
        data_source = str(request.get("data_source") or "gfs").lower()
        task_id = self.new_task_id(data_source)
        task_dir = self.settings.run_dir / task_id
        (task_dir / "raw").mkdir(parents=True, exist_ok=False)
        (task_dir / "task.request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.create(task_id, request, owner_sub)
        task = self._merge_runtime(
            task_id,
            attempt_started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            requested_runtime_profile=FIXED_RUNTIME_PROFILE,
            active_runtime_profile=FIXED_RUNTIME_PROFILE,
            fallback_used=False,
        )
        self._dispatch(task_id)
        self._log(task_id, f"{data_source.upper()} 任务已提交，开始动态并行调度")
        return task

    @staticmethod
    def _decorate_legacy_failure(task: dict[str, Any]) -> dict[str, Any]:
        if task.get("failure") or task.get("status") != "failed" or not task.get("error"):
            return task
        message = str(task.get("error") or "")
        lowered = message.lower()
        runtime = task.get("runtime") or {}
        if any(marker in lowered for marker in EXTERNAL_ERROR_MARKERS):
            failure_class, action = "external", "resume"
        elif runtime.get("remote_wrf_succeeded"):
            failure_class, action = "output", "retry_outputs"
        else:
            failure_class, action = "model", "edit_and_restart"
        task["failure"] = {
            "failure_class": failure_class,
            "stage": task.get("stage") or "unknown",
            "code": "legacy_error",
            "message": message,
            "recoverable": True,
            "recommended_action": action,
            "derived": True,
        }
        return task

    def _attempt_summaries(self, task_id: str) -> list[dict[str, Any]]:
        attempts = getattr(self.store, "attempts", None)
        if not attempts:
            return []
        return [
            {
                key: item.get(key)
                for key in (
                    "attempt_no", "status", "stage", "progress", "failure", "error",
                    "started_at", "finished_at",
                )
            }
            for item in attempts(task_id)
        ]

    def get(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        task = self._decorate_legacy_failure(task)
        task["attempts"] = self._attempt_summaries(task_id)
        return task

    def list(self, limit: int = 50, owner_sub: str | None = None) -> list[dict[str, Any]]:
        tasks = self.store.list(limit, owner_sub)
        for task in tasks:
            task["attempts"] = self._attempt_summaries(task["id"])
        return [self._decorate_legacy_failure(task) for task in tasks]

    @staticmethod
    def _forcing_cycle(runtime: dict[str, Any]) -> str | None:
        return runtime.get("forcing_cycle") or runtime.get("gfs_cycle") or runtime.get("ecmwf_cycle")

    def active_forcing_cycles(self, data_source: str) -> set[str]:
        source = str(data_source or "gfs").lower()
        tasks = list(self.store.active())
        paused = getattr(self.store, "with_status", lambda *_args: [])("paused_external")
        tasks.extend(paused)
        return {
            str(self._forcing_cycle(task.get("runtime") or {}))
            for task in tasks
            if str((task.get("request") or {}).get("data_source") or "gfs") == source
            and self._forcing_cycle(task.get("runtime") or {})
        }

    def active_gfs_cycles(self) -> set[str]:
        return self.active_forcing_cycles("gfs")

    def retained_gfs_cycles(self) -> set[str]:
        if self.gfs is None:
            return set()
        return set(self.gfs.latest_cycles(count=self.settings.hpc_gfs_retained_cycles))

    def protected_gfs_cycles(self) -> set[str]:
        return self.active_gfs_cycles() | self.retained_gfs_cycles()

    def pending_gfs_demand(self) -> dict[str, Any]:
        waiting_stages = {
            "queued",
            "selecting_cycle",
            "checking_hpc_gfs",
            "waiting_for_hpc_gfs",
        }
        tasks = [
            task
            for task in self.store.active()
            if task.get("status") in {"queued", "prefetching"}
            or task.get("stage") in waiting_stages
        ]
        cycles = sorted({
            str((task.get("runtime") or {}).get("gfs_cycle"))
            for task in tasks
            if (task.get("runtime") or {}).get("gfs_cycle")
        })
        return {"task_count": len(tasks), "cycles": cycles}

    def retry(self, task_id: str) -> dict[str, Any]:
        self.get(task_id)
        raise TaskConflictError("旧重试入口已停用；请先获取 restart-plan、核对精确路径，再提交 restart")

    def _restart_local_paths(self, task_id: str) -> list[Path]:
        run_dir = self._managed_task_path(self.settings.run_dir, task_id)
        output_dir = self._managed_task_path(self.settings.output_dir / "runs", task_id)
        return [
            run_dir / "raw",
            output_dir,
            run_dir / "service.log",
            run_dir / "task.config.json",
            run_dir / "task.env",
            run_dir / "gfs.expected.tsv",
        ]

    def restart_plan(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task["status"] not in {"waiting_restart", "failed", "cancelled"}:
            raise TaskConflictError("只有已失败、待调整或已取消的任务可以清理后重跑")
        runtime = task.get("runtime") or {}
        if runtime.get("remote_wrf_succeeded"):
            raise TaskConflictError("远端 WRF 已完成，请恢复结果下载，不要清理重跑")
        status = self.hpc.status(task_id)
        remote_status = str(status.get("status") or "unknown")
        allow_missing = not runtime.get("remote_pid") and not runtime.get("remote_launch_attempted")
        can_restart = remote_status == "failed" or (remote_status == "missing" and allow_missing)
        reason = None
        if remote_status == "running":
            reason = "远端任务仍在运行"
        elif remote_status == "succeeded":
            reason = "远端任务已成功，应恢复结果下载"
        elif remote_status == "lost":
            reason = "远端进程状态不明"
        elif remote_status == "missing" and not allow_missing:
            reason = "任务曾启动远端进程，但远端记录缺失"
        remote_paths = getattr(self.hpc, "task_artifact_paths", lambda _task_id: [])(task_id)
        return {
            "task_id": task_id,
            "attempt_no": int(task.get("attempt_no") or 1),
            "remote_status": remote_status,
            "can_restart": can_restart,
            "reason": reason,
            "local_paths": [str(path) for path in self._restart_local_paths(task_id)],
            "remote_paths": remote_paths,
            "preserved": [str(self.settings.run_dir / task_id / "attempts"), self.settings.hpc_gfs_dir],
        }

    def _archive_attempt_files(self, task: dict[str, Any]) -> Path:
        task_id = task["id"]
        attempt_no = int(task.get("attempt_no") or 1)
        run_dir = self._managed_task_path(self.settings.run_dir, task_id)
        attempt_dir = run_dir / "attempts" / f"{attempt_no:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        log_path = run_dir / "service.log"
        archived_log = attempt_dir / "service.log"
        if log_path.exists():
            shutil.copy2(log_path, archived_log)
        snapshot = {
            key: task.get(key)
            for key in ("id", "attempt_no", "status", "stage", "progress", "request", "runtime", "result", "failure", "error")
        }
        (attempt_dir / "attempt.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return archived_log

    def restart(
        self,
        task_id: str,
        request: dict[str, Any],
        *,
        confirm_task_id: str,
        confirm_attempt: int,
    ) -> dict[str, Any]:
        request = normalize_task_request(request)
        task = self.get(task_id)
        if confirm_task_id != task_id or confirm_attempt != int(task.get("attempt_no") or 1):
            raise ValueError("任务或尝试次数确认值已过期，请重新获取清理清单")
        plan = self.restart_plan(task_id)
        if not plan["can_restart"]:
            raise TaskConflictError(plan.get("reason") or "当前远端状态不允许清理重跑")
        if (task.get("failure") or {}).get("derived"):
            persisted_failure = dict(task["failure"])
            persisted_failure.pop("derived", None)
            self.store.update(task_id, failure=persisted_failure)
            task["failure"] = persisted_failure
        archived_log = self._archive_attempt_files(task)
        archive_attempt = getattr(self.store, "archive_attempt", None)
        if archive_attempt:
            archive_attempt(task_id, str(archived_log) if archived_log.exists() else None)
        runtime = task.get("runtime") or {}
        cleanup = getattr(self.hpc, "cleanup_task_attempt", None)
        if cleanup:
            cleanup(
                task_id,
                allow_missing=not runtime.get("remote_pid") and not runtime.get("remote_launch_attempted"),
            )
        for path in self._restart_local_paths(task_id):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        run_dir = self._managed_task_path(self.settings.run_dir, task_id)
        (run_dir / "raw").mkdir(parents=True, exist_ok=True)
        (run_dir / "task.request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        begin_attempt = getattr(self.store, "begin_attempt", None)
        if not begin_attempt:
            raise RuntimeError("任务存储不支持同任务重跑")
        with self._state_lock:
            self._cancelled.discard(task_id)
        restarted = begin_attempt(task_id, request)
        self._dispatch(task_id)
        self._log(task_id, f"已清理任务级残片并开始第 {restarted['attempt_no']} 次尝试；共享 GFS 数据池已保留")
        return self.get(task_id)

    def resume_external(self, task_id: str, *, automatic: bool = False) -> dict[str, Any]:
        task = self.get(task_id)
        failure = task.get("failure") or {}
        if task["status"] not in {"paused_external", "reconciling", "failed"}:
            raise TaskConflictError("当前任务不处于可恢复的外部中断状态")
        if task["status"] == "failed" and failure.get("failure_class") != "external":
            raise TaskConflictError("该失败不是外部连接故障，不能断点继续")
        if task["status"] == "reconciling":
            return task
        with self._state_lock:
            timer = self._reconcile_timers.pop(task_id, None)
            if timer:
                timer.cancel()
        runtime = dict(task.get("runtime") or {})
        runtime["external_retry_started_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        runtime["external_retry_count"] = 0
        self.store.update(
            task_id,
            status="reconciling",
            stage="reconciling",
            runtime=runtime,
            failure={**failure, "failure_class": "external", "recoverable": True, "recommended_action": "resume"},
            error=None,
        )
        self._dispatch(task_id)
        self._log(task_id, "tx-lab 连接恢复，自动继续原任务对账" if automatic else "用户请求继续任务，开始与 tx-lab 对账")
        return self.get(task_id)

    def retry_outputs(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task["status"] != "failed":
            raise TaskConflictError("只有失败任务可以重试结果下载")
        runtime = dict(task.get("runtime") or {})
        if not runtime.get("remote_wrf_succeeded") or not runtime.get("gfs_cycle"):
            raise TaskConflictError("远端 WRF 尚未成功完成，不能仅重试结果下载")
        runtime["retry_outputs_only"] = True
        with self._state_lock:
            self._cancelled.discard(task_id)
        task = self.store.update(
            task_id,
            status="queued",
            stage="retrying_outputs",
            progress=88,
            runtime=runtime,
            error=None,
        )
        self._dispatch(task_id)
        self._log(task_id, "已开始结果恢复动态调度，仅重试下载和渲染，不重新运行 WPS/WRF")
        return task

    def render_partial(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        runtime = dict(task.get("runtime") or {})
        if task["status"] != "failed":
            raise TaskConflictError("只有结果完整性校验失败的任务可以部分渲染")
        if not runtime.get("remote_wrf_succeeded") or not runtime.get("gfs_cycle"):
            raise TaskConflictError("远端 WRF 尚未成功完成，不能部分渲染")
        known_invalid = (runtime.get("output_validation") or {}).get("invalid")
        legacy_render_error = any(
            marker in str(task.get("error") or "").lower()
            for marker in ("netcdf", "hdf", "wrfout", "unknown file format")
        )
        if not known_invalid and not legacy_render_error:
            raise TaskConflictError("任务没有已确认的不可读帧，不能切换为部分渲染")
        runtime.update(retry_outputs_only=True, allow_partial_outputs=True)
        with self._state_lock:
            self._cancelled.discard(task_id)
        task = self.store.update(
            task_id,
            status="queued",
            stage="retrying_partial_render",
            progress=88,
            runtime=runtime,
            error=None,
        )
        self._dispatch(task_id)
        self._log(task_id, "用户确认忽略不可读帧，已开始部分结果渲染动态调度")
        return task

    @staticmethod
    def _managed_task_path(root: Path, task_id: str) -> Path:
        base = root.resolve()
        candidate = (base / task_id).resolve()
        if Path(task_id).name != task_id or candidate.parent != base:
            raise ValueError("任务目录不在受管路径内")
        return candidate

    def delete_local(self, task_id: str, confirm_task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if confirm_task_id != task_id:
            raise ValueError("确认任务 ID 与待删除任务不一致")
        if task["status"] not in FINAL_STATES:
            raise TaskConflictError("运行中任务不能删除，请先取消并等待状态确认")
        run_path = self._managed_task_path(self.settings.run_dir, task_id)
        output_path = self._managed_task_path(self.settings.output_dir / "runs", task_id)
        removed = {"run_dir": False, "output_dir": False, "task_record": False}
        if run_path.exists():
            shutil.rmtree(run_path)
            removed["run_dir"] = True
        if output_path.exists():
            shutil.rmtree(output_path)
            removed["output_dir"] = True
        removed["task_record"] = self.store.delete(task_id)
        with self._state_lock:
            self._cancelled.discard(task_id)
        return {"task_id": task_id, "removed": removed}

    def _log_path(self, task_id: str) -> Path:
        return self.settings.run_dir / task_id / "service.log"

    def _log(self, task_id: str, message: str) -> None:
        path = self._log_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with path.open("a", encoding="utf-8") as output:
            output.write(f"[{stamp}] {message.rstrip()}\n")

    def logs(
        self,
        task_id: str,
        after: int = 0,
        limit: int = 65536,
        attempt_no: int | None = None,
    ) -> dict[str, Any]:
        task = self.get(task_id)
        current_attempt = int(task.get("attempt_no") or 1)
        path = self._log_path(task_id)
        if attempt_no is not None and attempt_no != current_attempt:
            attempts = getattr(self.store, "attempts", lambda _task_id: [])(task_id)
            archived = next(
                (item for item in attempts if int(item.get("attempt_no") or 0) == attempt_no),
                None,
            )
            if not archived or not archived.get("log_path"):
                raise TaskNotFoundError(f"{task_id}/attempt/{attempt_no}")
            candidate = Path(str(archived["log_path"])).resolve()
            attempts_root = (self.settings.run_dir / task_id / "attempts").resolve()
            if attempts_root not in candidate.parents:
                raise ValueError("历史日志不在任务归档目录内")
            path = candidate
        if not path.exists():
            return {"offset": 0, "text": ""}
        size = path.stat().st_size
        start = min(max(0, int(after)), size)
        with path.open("rb") as source:
            source.seek(start)
            data = source.read(max(1, min(int(limit), 262144)))
            offset = source.tell()
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text)
        text = "".join(char for char in text if char in "\n\r\t" or ord(char) >= 32)
        return {"offset": offset, "text": text}

    def _merge_runtime(self, task_id: str, **values: Any) -> dict[str, Any]:
        task = self.get(task_id)
        runtime = dict(task.get("runtime") or {})
        runtime.update(values)
        return self.store.update(task_id, runtime=runtime)

    @staticmethod
    def _is_external_error(exc: Exception) -> bool:
        if isinstance(exc, RemoteTaskFailure):
            return False
        if isinstance(exc, HpcError):
            message = str(exc).lower()
            return any(marker in message for marker in EXTERNAL_ERROR_MARKERS)
        return False

    def _failure_info(self, task_id: str, exc: Exception) -> dict[str, Any]:
        task = self.get(task_id)
        runtime = task.get("runtime") or {}
        message = str(exc)
        lowered = message.lower()
        remote_failure = exc.remote_status.get("failure") if isinstance(exc, RemoteTaskFailure) else None
        stage = str((remote_failure or {}).get("stage") or task.get("stage") or "unknown")
        if self._is_external_error(exc):
            failure_class, action, recoverable = "external", "resume", True
        elif runtime.get("remote_wrf_succeeded") or stage in {"downloading_outputs", "rendering", "checking_remote_outputs"}:
            failure_class, action, recoverable = "output", "retry_outputs", True
        elif stage in {"selecting_cycle", "checking_hpc_gfs", "waiting_for_hpc_gfs", "remote_gfs_ready", "data"}:
            failure_class, action, recoverable = "data", "restart", True
        elif any(marker in lowered for marker in ("namelist", "parameter", "参数", "配置", "mismatch", "inconsisten")):
            failure_class, action, recoverable = "configuration", "edit_and_restart", True
        else:
            failure_class, action, recoverable = "model", "edit_and_restart", True
        return {
            "failure_class": failure_class,
            "stage": stage,
            "code": str((remote_failure or {}).get("exit_code") or getattr(exc, "remote_status", {}).get("exit_code") or ""),
            "message": message,
            "recoverable": recoverable,
            "recommended_action": action,
            "remote": remote_failure,
        }

    def _schedule_reconcile(self, task_id: str) -> None:
        delay = self.settings.hpc_reconcile_interval_seconds

        def enqueue() -> None:
            with self._state_lock:
                self._reconcile_timers.pop(task_id, None)
            if self._stop.is_set():
                return
            task = self.store.get(task_id)
            if task and task.get("status") == "reconciling":
                self._dispatch(task_id)

        with self._state_lock:
            previous = self._reconcile_timers.pop(task_id, None)
            if previous:
                previous.cancel()
            timer = threading.Timer(delay, enqueue)
            timer.daemon = True
            timer.name = f"wrf-reconcile-{task_id[-8:]}"
            self._reconcile_timers[task_id] = timer
            timer.start()

    def _defer_external(self, task_id: str, exc: Exception) -> None:
        task = self.get(task_id)
        runtime = dict(task.get("runtime") or {})
        now = datetime.now(timezone.utc)
        started_text = runtime.get("external_retry_started_at")
        try:
            started = datetime.fromisoformat(str(started_text).replace("Z", "+00:00")) if started_text else now
        except ValueError:
            started = now
        runtime["external_retry_started_at"] = started.isoformat().replace("+00:00", "Z")
        runtime["external_retry_count"] = int(runtime.get("external_retry_count") or 0) + 1
        failure = self._failure_info(task_id, exc)
        elapsed = (now - started).total_seconds()
        if elapsed >= self.settings.hpc_reconcile_timeout_seconds:
            self.store.update(
                task_id,
                status="paused_external",
                stage="paused_external",
                runtime=runtime,
                failure=failure,
                error=str(exc),
            )
            minutes = max(1, self.settings.hpc_reconcile_timeout_seconds // 60)
            self._log(task_id, f"外部连接连续 {minutes} 分钟不可用，任务已暂停；重新认证后可从原阶段继续")
            return
        self.store.update(
            task_id,
            status="reconciling",
            stage="reconciling",
            runtime=runtime,
            failure=failure,
            error=str(exc),
        )
        self._log(task_id, f"外部连接暂不可用，将在 {self.settings.hpc_reconcile_interval_seconds} 秒后继续对账：{exc}")
        self._schedule_reconcile(task_id)

    def _is_cancelled(self, task_id: str) -> bool:
        with self._state_lock:
            return task_id in self._cancelled

    def _raise_if_cancelled(self, task_id: str) -> None:
        if self._is_cancelled(task_id):
            raise TaskCancelledError("任务已取消")

    def _cycle_lock(self, cycle: str, data_source: str = "gfs") -> threading.Lock:
        with self._state_lock:
            return self._cycle_locks.setdefault(f"{data_source}:{cycle}", threading.Lock())

    @contextmanager
    def _prepare_cycle(self, task_id: str, cycle: str, data_source: str = "gfs"):
        label = data_source.upper()
        lock = self._cycle_lock(cycle, data_source)
        if lock.locked():
            self.store.update(task_id, stage="waiting_for_forcing_cache", progress=4)
            self._log(task_id, f"等待其他任务完成 {label} {cycle} 缓存准备")
        while not lock.acquire(timeout=0.5):
            if self._is_cancelled(task_id):
                raise TaskCancelledError(f"等待 {label} 缓存期间任务已取消")
            if self._stop.is_set():
                raise RuntimeError(f"等待 {label} 缓存期间服务正在停止")
        try:
            self._raise_if_cancelled(task_id)
            if self._stop.is_set():
                raise RuntimeError(f"{label} 缓存准备开始前服务正在停止")
            yield
        finally:
            lock.release()

    def _remote_cancel(self, task_id: str) -> None:
        try:
            result = self.hpc.cancel(task_id)
            self._log(task_id, f"远端取消请求：{result['status']}")
            if result["status"] in {"term_sent", "not_running"}:
                self.store.update(
                    task_id,
                    status="cancelled",
                    stage="cancelled",
                    error=None,
                )
            else:
                self.store.update(
                    task_id,
                    status="cancel_pending",
                    stage="cancel_pending",
                    error=f"远端取消状态尚未确认：{result['status']}",
                )
        except Exception as exc:
            self._log(task_id, f"远端取消尚未确认：{exc}")
            self.store.update(
                task_id,
                status="cancel_pending",
                stage="cancel_pending",
                error=f"远端取消尚未确认：{exc}",
            )
        finally:
            with self._state_lock:
                current = self._cancel_threads.get(task_id)
                if current is threading.current_thread():
                    self._cancel_threads.pop(task_id, None)

    def _start_remote_cancel(self, task_id: str) -> None:
        with self._state_lock:
            existing = self._cancel_threads.get(task_id)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._remote_cancel,
                args=(task_id,),
                daemon=True,
                name=f"wrf-cancel-{task_id[-8:]}",
            )
            self._cancel_threads[task_id] = thread
        thread.start()

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task["status"] in FINAL_STATES:
            return task
        with self._state_lock:
            self._cancelled.add(task_id)
        remote_pid = (task.get("runtime") or {}).get("remote_pid")
        if not remote_pid:
            self._log(task_id, "任务在远端进程启动前取消")
            return self.store.update(
                task_id,
                status="cancelled",
                stage="cancelled",
                progress=task["progress"],
                error=None,
            )
        self.store.update(
            task_id,
            status="cancel_pending",
            stage="cancel_pending",
            error=None,
        )
        self._start_remote_cancel(task_id)
        return self.get(task_id)

    def _complete_task_cancellation(self, task_id: str) -> None:
        task = self.get(task_id)
        if task["status"] in FINAL_STATES:
            return
        if (task.get("runtime") or {}).get("remote_pid"):
            self.store.update(task_id, status="cancel_pending", stage="cancel_pending")
            self._start_remote_cancel(task_id)
            self._log(task_id, "本地工作线程已停止，等待远端取消确认")
            return
        self.store.update(task_id, status="cancelled", stage="cancelled", error=None)
        self._log(task_id, "任务已在远端进程启动前停止")

    def _run_task(self, task_id: str) -> None:
        current = threading.current_thread()
        try:
            task = self.get(task_id)
            if task["status"] in FINAL_STATES or self._is_cancelled(task_id):
                return
            with self._state_lock:
                self._pending_task_ids.discard(task_id)
                self._active_task_ids.add(task_id)
            self._execute(task_id)
        except TaskCancelledError:
            self._complete_task_cancellation(task_id)
        except Exception as exc:
            if self._is_cancelled(task_id):
                self._complete_task_cancellation(task_id)
            elif self._is_external_error(exc):
                self._defer_external(task_id, exc)
            else:
                self._log(task_id, f"任务失败：{exc}")
                failure = self._failure_info(task_id, exc)
                status = "failed" if failure["failure_class"] == "output" else "waiting_restart"
                self.store.update(
                    task_id,
                    status=status,
                    stage="failed",
                    failure=failure,
                    error=str(exc),
                )
        finally:
            with self._state_lock:
                self._pending_task_ids.discard(task_id)
                self._active_task_ids.discard(task_id)
                if self._task_threads.get(task_id) is current:
                    self._task_threads.pop(task_id, None)

    def _reconcile(self, task_id: str) -> str:
        self._log(task_id, "开始与 tx-lab 任务对账")
        self._raise_if_cancelled(task_id)
        status = self.hpc.status(task_id)
        self._merge_remote_profile(task_id, status)
        if status["status"] == "running":
            self.store.update(task_id, status="running", stage="running", failure=None, error=None)
            return "monitor"
        if status["status"] == "succeeded":
            return "finalize"
        if status["status"] == "failed":
            raise RemoteTaskFailure(status)
        task = self.get(task_id)
        runtime = task.get("runtime") or {}
        if status["status"] == "missing" and not runtime.get("remote_pid") and not runtime.get("remote_launch_attempted"):
            self._log(task_id, "远端尚未启动该任务，重新执行幂等的数据准备流程")
            return "restart"
        failure = {
            "failure_class": "model",
            "stage": task.get("stage") or "unknown",
            "code": f"remote_{status['status']}",
            "message": "远端进程状态不明，已禁止自动清理和重复启动",
            "recoverable": True,
            "recommended_action": "edit_and_restart",
        }
        self.store.update(
            task_id,
            status="waiting_restart",
            stage="failed",
            failure=failure,
            error=failure["message"],
        )
        self._log(task_id, failure["message"])
        return "stop"

    def _monitor(self, task_id: str) -> bool:
        last_log = ""
        while not self._stop.is_set():
            self._raise_if_cancelled(task_id)
            status = self.hpc.status(task_id)
            self._merge_remote_profile(task_id, status)
            remote_log = status.get("log", "")
            if remote_log and remote_log != last_log:
                self._log(task_id, "--- 远端日志快照 ---\n" + remote_log)
                last_log = remote_log
            if status["status"] == "succeeded":
                return True
            if status["status"] == "failed":
                raise RemoteTaskFailure(status)
            if status["status"] not in {"running"}:
                raise RuntimeError(f"远端任务状态异常：{status['status']}")
            self._stop.wait(self.settings.hpc_poll_seconds)
        return False

    def _merge_remote_profile(self, task_id: str, status: dict[str, Any]) -> None:
        profile = status.get("runtime_profile") or {}
        if not isinstance(profile, dict) or not profile.get("actual"):
            return
        self._merge_runtime(
            task_id,
            requested_runtime_profile=profile.get("requested", "cpu"),
            active_runtime_profile=profile["actual"],
            fallback_used=bool(profile.get("fallback_used")),
            fallback_reason="gpu_runtime_error" if profile.get("fallback_used") else None,
        )

    def _finalize(self, task_id: str, cycle: str, *, allow_partial: bool = False) -> None:
        self._raise_if_cancelled(task_id)
        task = self.get(task_id)
        self._merge_runtime(task_id, remote_wrf_succeeded=True)
        validator = getattr(self.hpc, "validate_outputs", None)
        validation = validator(task_id) if validator else {"valid": [], "invalid": [], "complete": True}
        self._merge_runtime(task_id, output_validation=validation)
        invalid_outputs = list(validation.get("invalid") or [])
        if invalid_outputs and not allow_partial:
            names = ", ".join(str(item.get("name") or "unknown") for item in invalid_outputs[:5])
            raise RuntimeError(f"tx-lab wrfout 完整性校验失败：{names}；可手动选择忽略不可读帧并部分渲染")
        valid_names = {str(item["name"]) for item in validation.get("valid") or [] if item.get("name")}
        run_dir = self.settings.run_dir / task_id
        raw_dir = run_dir / "raw"
        self.store.update(task_id, status="rendering", stage="downloading_outputs", progress=88)
        self._log(task_id, "下载 tx-lab wrfout 结果")
        last_download_progress = 88

        def download_progress(done: int, total: int) -> None:
            nonlocal last_download_progress
            self._raise_if_cancelled(task_id)
            value = 93 if total > 0 and done >= total else 88
            if total > 0 and done < total:
                value = 88 + min(5, int(done * 5 / total))
            if value > last_download_progress:
                last_download_progress = value
                self.store.update(
                    task_id,
                    status="rendering",
                    stage="downloading_outputs",
                    progress=value,
                )

        if invalid_outputs:
            self.hpc.download_outputs(
                task_id,
                raw_dir,
                progress=download_progress,
                include_names=valid_names,
            )
        else:
            self.hpc.download_outputs(task_id, raw_dir, progress=download_progress)
        self._raise_if_cancelled(task_id)
        self.store.update(task_id, stage="rendering", progress=94)
        output_dir = self.settings.output_dir / "runs" / task_id
        if allow_partial:
            manifest = render_run(
                task_id,
                raw_dir,
                output_dir,
                task["request"],
                cycle,
                allow_partial=True,
                excluded_outputs=invalid_outputs,
            )
        else:
            manifest = render_run(task_id, raw_dir, output_dir, task["request"], cycle)
        self._raise_if_cancelled(task_id)
        result = {
            "task_id": task_id,
            "meta_url": f"/data/WRF/runs/{task_id}/scene.meta.json",
            "meta_json": manifest,
        }
        partial = manifest.get("quality", {}).get("status") == "partial"
        final_status = "partial_success" if partial else "succeeded"
        self.store.update(
            task_id,
            status=final_status,
            stage="done",
            progress=100,
            result=result,
            failure=None,
            error=None,
        )
        self._log(task_id, "任务部分完成，不可读帧已排除并记录质量告警" if partial else "任务完成，WebP 与 scene.meta.json 已生成")

    def _execute(self, task_id: str) -> None:
        task = self.get(task_id)
        runtime = task.get("runtime") or {}
        if runtime.get("retry_outputs_only"):
            self.store.update(
                task_id,
                status="rendering",
                stage="checking_remote_outputs",
                progress=88,
                error=None,
            )
            self._log(task_id, "确认远端 WRF 已成功，准备断点续传 wrfout")
            remote_status = self.hpc.status(task_id)
            if remote_status.get("status") != "succeeded":
                raise RuntimeError(
                    f"远端 WRF 尚未成功完成，不能仅恢复结果：{remote_status.get('status', 'unknown')}"
                )
            cycle = runtime.get("gfs_cycle")
            if not cycle:
                raise RuntimeError("恢复结果下载缺少 GFS cycle")
            self._merge_runtime(
                task_id,
                retry_outputs_only=False,
                remote_wrf_succeeded=True,
            )
            if runtime.get("allow_partial_outputs"):
                self._finalize(task_id, cycle, allow_partial=True)
            else:
                self._finalize(task_id, cycle)
            return
        if task["status"] == "reconciling":
            action = self._reconcile(task_id)
            if action == "stop":
                return
            if action == "monitor":
                if not self._monitor(task_id):
                    return
                self._merge_runtime(task_id, remote_wrf_succeeded=True)
                task = self.get(task_id)
                cycle = (task.get("runtime") or {}).get("gfs_cycle")
                if not cycle:
                    raise RuntimeError("恢复任务缺少 GFS cycle")
                self._finalize(task_id, cycle)
                return
            if action == "finalize":
                self._merge_runtime(task_id, remote_wrf_succeeded=True)
                cycle = runtime.get("gfs_cycle")
                if not cycle:
                    raise RuntimeError("恢复任务缺少 GFS cycle")
                self._finalize(task_id, cycle)
                return

        request = task["request"]
        if not runtime.get("remote_pid") and not runtime.get("remote_launch_attempted"):
            normalized_request = normalize_task_request(request)
            if normalized_request != request:
                request = normalized_request
                self.store.update(task_id, request=request)
                request_path = self.settings.run_dir / task_id / "task.request.json"
                if request_path.parent.is_dir():
                    request_path.write_text(
                        json.dumps(request, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                self._log(task_id, "未启动任务已按当前策略固定为 CPU 与 6 小时 spin-up")
        requested_profile = FIXED_RUNTIME_PROFILE
        self._merge_runtime(
            task_id,
            requested_runtime_profile=requested_profile,
            active_runtime_profile=requested_profile,
            fallback_used=False,
            fallback_reason=None,
        )
        data_source = str(request.get("data_source") or "gfs").lower()
        source_label = data_source.upper()
        start = datetime.fromisoformat(request["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(request["end_time"].replace("Z", "+00:00"))
        spinup_hours = resolve_spinup_hours(request)
        model_start = start - timedelta(hours=spinup_hours)
        self.store.update(
            task_id,
            status="prefetching",
            stage="selecting_cycle",
            progress=2,
            failure=None,
            error=None,
        )
        self._log(task_id, f"产品起报前增加 {spinup_hours} 小时 spin-up；按固定 00Z 策略选择 {source_label} cycle")
        self._raise_if_cancelled(task_id)
        cycle, _ = self.gfs.select_cycle(model_start, end, data_source=data_source)
        self._raise_if_cancelled(task_id)
        interval = int(request.get("forecast_interval_hours") or 1)
        hours = self.gfs.required_hours(model_start, end, interval, cycle, data_source=data_source)
        self._merge_runtime(
            task_id,
            data_source=data_source,
            forcing_cycle=cycle,
            **{f"{data_source}_cycle": cycle},
            gfs_cycle=cycle,
            forecast_hours=hours,
            spinup_hours=spinup_hours,
            model_start_time=model_start.isoformat().replace("+00:00", "Z"),
        )

        with self._prepare_cycle(task_id, cycle, data_source):
            self.store.update(task_id, stage="checking_hpc_forcing", progress=4)
            self._log(task_id, f"检查 tx-lab {source_label} 数据池：{cycle}，任务间隔 {interval} 小时，共 {len(hours)} 个时次")
            last_ready = -1
            logged_cleanup_paths: set[str] = set()

            def remote_forcing_progress(snapshot: dict[str, Any]) -> None:
                nonlocal last_ready
                deleted_paths = set((snapshot.get("auto_cleanup") or {}).get("deleted") or [])
                new_cleanup_paths = sorted(deleted_paths - logged_cleanup_paths)
                if new_cleanup_paths:
                    logged_cleanup_paths.update(new_cleanup_paths)
                    self._log(task_id, f"已自动清理旧 {source_label} 周期：{', '.join(new_cleanup_paths)}")
                ready = len(snapshot.get("valid_hours") or [])
                download = snapshot.get("download") or {}
                if ready != last_ready:
                    last_ready = ready
                    self._log(task_id, f"等待 tx-lab 共享 {source_label} 下载：{ready}/{len(hours)} 个任务时次已就绪")
                self._merge_runtime(
                    task_id,
                    forcing_remote_reused=ready,
                    forcing_remote_missing=len(snapshot.get("missing_hours") or []),
                    forcing_total=len(hours),
                    forcing_download_owner="hpc_shared_pool",
                    gfs_remote_reused=ready,
                    gfs_remote_missing=len(snapshot.get("missing_hours") or []),
                    gfs_total=len(hours),
                    gfs_download_owner="hpc_shared_pool",
                    gfs_download_state=download.get("status"),
                    gfs_download_detail=download.get("detail"),
                )
                self.store.update(
                    task_id,
                    progress=5 + int(55 * ready / max(1, len(hours))),
                    stage="waiting_for_hpc_forcing",
                )

            ensure_remote = getattr(self.hpc, "ensure_remote_forcing", None)
            if ensure_remote:
                remote_cache = ensure_remote(
                    data_source,
                    cycle,
                    hours,
                    progress=remote_forcing_progress,
                    cancelled=lambda: self._is_cancelled(task_id) or self._stop.is_set(),
                    protected_cycles=self.protected_gfs_cycles() if data_source == "gfs" else self.active_forcing_cycles(data_source),
                )
            else:
                remote_cache = self.hpc.inspect_gfs_files(cycle, hours)
                if remote_cache.get("missing_hours"):
                    raise RuntimeError(f"tx-lab {source_label} 数据池不完整，且连接客户端不支持远端共享下载")
            verified_entries = list(remote_cache.get("entries") or [])
            self._merge_runtime(
                task_id,
                forcing_remote_reused=len(remote_cache.get("valid_hours") or []),
                forcing_remote_missing=0,
                forcing_total=len(hours),
                forcing_download_owner="hpc_shared_pool",
                gfs_remote_reused=len(remote_cache.get("valid_hours") or []),
                gfs_remote_missing=0,
                gfs_total=len(hours),
                gfs_download_owner="hpc_shared_pool",
            )
            self._log(task_id, f"tx-lab 完整 {source_label} 文件校验通过，任务将直接复用共享数据池")
            self.store.update(task_id, progress=65, stage="remote_forcing_ready")
            self._raise_if_cancelled(task_id)

        last_transfer_status: tuple[str, str] | None = None

        def sync_transfer_status() -> None:
            nonlocal last_transfer_status
            transfer = getattr(self.hpc, "transfer_status", None)
            if not transfer:
                return
            marker = (str(transfer.get("mode") or ""), str(transfer.get("message") or ""))
            if marker == last_transfer_status:
                return
            last_transfer_status = marker
            self._merge_runtime(task_id, hpc_transfer=transfer)
            self._log(task_id, f"tx-lab 文件传输：{marker[0]} - {marker[1]}")

        self._raise_if_cancelled(task_id)
        run_dir = self.settings.run_dir / task_id
        config_path = run_dir / "task.config.json"
        environment_path = run_dir / "task.env"
        expected_forcing_path = run_dir / "forcing.expected.tsv"
        write_task_bundle(
            task_id,
            request,
            cycle,
            hours,
            verified_entries,
            config_path,
            environment_path,
            expected_forcing_path,
        )
        self.store.update(task_id, status="uploading", stage="preparing_hpc", progress=66)
        self._log(task_id, "上传任务配置与 WRF 运行脚本")

        def prepare_progress(name: str, done: int, total: int) -> None:
            self._raise_if_cancelled(task_id)
            sync_transfer_status()
            self._merge_runtime(
                task_id,
                hpc_preparing_file=name,
                hpc_prepared_bytes=done,
                hpc_prepare_bytes_total=total,
            )

        try:
            self.hpc.prepare_runtime(
                task_id,
                config_path,
                environment_path,
                expected_forcing_path,
                Path(__file__).resolve().parent / "scripts",
                progress=prepare_progress,
            )
        finally:
            sync_transfer_status()

        self._raise_if_cancelled(task_id)
        self.store.update(task_id, status="running", stage="running", progress=68)
        self._merge_runtime(task_id, remote_launch_attempted=True)
        launch = self.hpc.launch(task_id, data_source) if hasattr(self.hpc, "inspect_forcing_files") else self.hpc.launch(task_id)
        self._merge_runtime(task_id, **launch)
        self._log(task_id, f"tx-lab 任务已启动，PID {launch['remote_pid']}")
        if self._is_cancelled(task_id):
            self.store.update(task_id, status="cancel_pending", stage="cancel_pending")
            self._start_remote_cancel(task_id)
            raise TaskCancelledError("任务在远端进程启动时收到取消请求")
        if not self._monitor(task_id):
            return
        self._merge_runtime(task_id, remote_wrf_succeeded=True)
        self._finalize(task_id, cycle)
