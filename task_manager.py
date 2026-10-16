from __future__ import annotations

from contextlib import nullcontext

import json
import hashlib
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any

from config import Settings
from catalog_publisher import publish_workbench_result
from database import TaskStore
from gfs import GfsManager, boundary_buffer_hours
from hpc import HpcClient, HpcError, write_task_bundle
from renderer import render_run
from schemas import FIXED_RUNTIME_PROFILE, FIXED_SPINUP_HOURS, normalize_task_request
from runtime_assets import archived_log_path, log_paths, read_log_bytes
from forcing_lifecycle import forcing_reference_guard


FINAL_STATES = {"succeeded", "partial_success", "failed", "cancelled", "waiting_restart"}
EXTERNAL_ERROR_MARKERS = (
    "堡垒", "会话", "连接", "认证", "密码", "shell", "timeout", "timed out",
    "connection", "disconnect", "broken pipe", "eof", "permission denied",
)


def resolve_spinup_hours(request: dict[str, Any]) -> int:
    return int((request.get("spinup") or {}).get("hours", FIXED_SPINUP_HOURS))


def apply_forcing_observations(actions: list[dict[str, Any]], pool: dict[str, Any]) -> None:
    """Use observed pool state for both sources, not enqueue acknowledgement."""
    cycles = {item["cycle"]: item for item in pool.get("cycles") or []}
    for action in actions:
        item = cycles.get(str(action.get("cycle") or ""))
        if not item:
            continue
        status = "ready" if item.get("complete") else item.get("status")
        if status not in {"ready", "downloading", "queued", "waiting_publish", "rate_limited", "retry_wait", "failed", "error"}:
            continue
        action["status"] = status
        for key in ("completed_files", "total_files", "size_bytes", "partial_size_bytes", "current_file", "download_rate_bps", "wait_reason"):
            if key in item:
                action[key] = item[key]
        if status == "ready":
            action["detail"] = f"{item.get('completed_files', item.get('total_files', 0))}/{item.get('total_files', 0)}"
        elif status == "downloading":
            action["detail"] = f"{item['current_file']} 下载中" if item.get("current_file") else "下载中，等待文件进度更新"
        else:
            action["detail"] = (item.get("wait_reason") or item.get("queue_message")
                                or item.get("download_message") or action.get("detail"))


def observed_model_progress(task: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Extract measured progress from one attempt, without estimating WPS work."""
    runtime = task.get("runtime") or {}
    values: dict[str, Any] = {}
    log = str(observation.get("log") or "")
    stages = {"prepare", "data", "workspace", "wps", "wrf", "done", "failed"}
    order = {name: index for index, name in enumerate(("prepare", "data", "workspace", "wps", "wrf", "done", "failed"))}
    current_stage = runtime.get("current_stage")
    for match in re.finditer(r"\[stage-status\]\s*(\{[^\n]*\})", log):
        try:
            record = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict) or not isinstance(record.get("stage"), str) or record["stage"] not in stages:
            continue
        stage = record["stage"]
        stage_status = {"running": "running", "done": "completed", "error": "failed"}.get(str(record.get("status")))
        if stage_status is None or order[stage] < order.get(current_stage, -1):
            continue
        current_stage = values["current_stage"] = stage
        values["current_stage_status"] = stage_status
    request = task.get("request") or {}
    try:
        start = datetime.fromisoformat(str(runtime.get("model_start_time") or request["start_time"]).replace("Z", "+00:00"))
        if not runtime.get("model_start_time"):
            start -= timedelta(hours=resolve_spinup_hours(request))
        end = datetime.fromisoformat(str(request["end_time"]).replace("Z", "+00:00"))
        start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start.astimezone(timezone.utc)
        end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None else end.astimezone(timezone.utc)
        observed = []
        for value in re.findall(r"Timing for main: time (\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d)", log):
            try:
                observed.append(datetime.strptime(value, "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc))
            except ValueError:
                continue
        previous = runtime.get("current_model_time")
        if previous:
            observed.append(datetime.fromisoformat(str(previous).replace("Z", "+00:00")))
        if observed and end > start:
            model_time = max(observed)
            values["current_model_time"] = model_time.isoformat().replace("+00:00", "Z")
            values["model_progress"] = round(max(0.0, min(100.0, (model_time - start).total_seconds() / (end - start).total_seconds() * 100)), 2)
            if current_stage not in {"done", "failed"} and values.get("current_stage_status") != "completed":
                values.update(current_stage="wrf", current_stage_status="running")
    except (KeyError, ValueError, TypeError, OverflowError):
        pass
    terminal = observation.get("status")
    if terminal in {"succeeded", "failed", "cancelled"}:
        values.update(current_stage="done" if terminal == "succeeded" else "failed",
                      current_stage_status="completed" if terminal == "succeeded" else "failed")
    return values


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


def serialized_task_operation(method):
    """Serialize user mutations for one task without holding the scheduler lock."""
    @wraps(method)
    def invoke(self, task_id, *args, **kwargs):
        with self._operation_lock(task_id):
            return method(self, task_id, *args, **kwargs)
    return invoke


def guard_legacy_action(method):
    """Protect the restored 1ee4c7b action body without duplicating execution."""
    @wraps(method)
    def invoke(self, task_id, *args, **kwargs):
        with self._operation_lock(task_id):
            if method.__name__ == "resume_external":
                task = self.get(task_id)
                if kwargs.get("automatic") and task["request"].get("recovery_policy") == "confirm":
                    return task
                if task["status"] == "reconciling":
                    return method(self, task_id, *args, **kwargs)
            self._require_recovery_idle(task_id)
            return method(self, task_id, *args, **kwargs)
    return invoke


class WrfTaskManager:
    def __init__(self, settings: Settings, store: TaskStore, gfs: GfsManager, hpc: HpcClient):
        self.settings = settings
        self.store = store
        self.gfs = gfs
        self.hpc = hpc
        self._stop = threading.Event()
        self._forcing_cleanup_wakeup = threading.Event()
        self._forcing_terminal_proofs = {}
        self._state_lock = threading.RLock()
        self._started = False
        self._task_threads: dict[str, threading.Thread] = {}
        self._resource_dispatch_thread = None
        self._pending_task_ids: set[str] = set()
        self._cancelled: set[str] = set()
        self._cancel_threads: dict[str, threading.Thread] = {}
        self._operation_locks: dict[str, Any] = {}
        self._cancel_timers: dict[str, threading.Timer] = {}
        self._cancel_observers: set[threading.Thread] = set()
        self._reconcile_timers: dict[str, threading.Timer] = {}
        self._active_task_ids: set[str] = set()
        self._cycle_locks: dict[str, threading.Lock] = {}
        self._gfs_sync_lock = threading.Lock()
        self._gfs_prefetch_thread: threading.Thread | None = None
        stored_policy = store.forcing_policy(settings.hpc_gfs_prefetch_enabled)
        prefetch_enabled = (stored_policy.get("enabled", settings.hpc_gfs_prefetch_enabled)
                            if isinstance(stored_policy, dict) else settings.hpc_gfs_prefetch_enabled)
        self._gfs_prefetch: dict[str, Any] = {
            "enabled": bool(prefetch_enabled and gfs is not None),
            "status": "idle" if prefetch_enabled else "disabled",
            "message": "等待后台检查最新 GFS、ECMWF 00Z"
            if prefetch_enabled
            else "GFS、ECMWF 后台预取已关闭",
            "interval_seconds": settings.hpc_gfs_prefetch_interval_seconds,
            "updated_at": None,
            "target_cycles": [],
        }
        self._health: dict[str, Any] = {"status": "checking", "message": "等待检查 tx-lab 连接"}

    def _operation_lock(self, task_id: str):
        with self._state_lock:
            return self._operation_locks.setdefault(task_id, threading.RLock())

    def _require_recovery_idle(self, task_id: str) -> None:
        if self.store.deletion_blocked(task_id):
            raise TaskConflictError("任务删除事务已开始，不能恢复或发布")
        with self._state_lock:
            if task_id in self._task_threads or task_id in self._cancel_threads:
                raise TaskConflictError("任务工作线程或取消核验尚未结束，请稍后重新获取计划")

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

    def set_forcing_policy(self, policy):
        enabled=bool(policy["enabled"] and self.gfs is not None)
        self._set_gfs_prefetch_status(enabled=enabled,status="idle" if enabled else "disabled",
            message="等待后台检查最新 GFS、ECMWF 00Z" if enabled else "后台预取已关闭")

    def refresh_hpc_health(self) -> dict[str, Any]:
        self._check_health()
        return self.hpc_health

    def authenticate_hpc(self, password: str) -> dict[str, Any]:
        self._health = self.hpc.authenticate_password(password)
        self._health["sampled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self._health.get("status") == "ready":
            self._resume_paused_tasks()
        return self.hpc_health

    def _check_health(self) -> None:
        self._health = self.hpc.health()
        self._health["sampled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self._health.get("status") == "ready":
            self._resume_paused_tasks()

    def _resource_dispatch_loop(self):
        while not self._stop.wait(5):
            try:
                if time.time() - getattr(self, '_resource_health_at', 0) >= 30:
                    self._check_health()
                    self._resource_health_at = time.time()
                self._dispatch_pending()
            except Exception:
                # Unknown capacity never permits a new reservation.
                self._health = {'status':'checking','message':'资源观测暂不可用，保持排队'}

    def _resume_paused_tasks(self) -> None:
        resumable = getattr(self.store, "with_status", lambda *_args: [])("paused_external")
        for task in resumable:
            try:
                if task["request"].get("recovery_policy") == "confirm":
                    observation = self.hpc.status(task["id"])
                    self._merge_runtime(task["id"], remote_observation={"status": observation.get("status"), "sampled_at": datetime.now(timezone.utc).isoformat()})
                    continue
                self.resume_external(task["id"], automatic=True)
            except (TaskConflictError, TaskNotFoundError, HpcError):
                continue

    def sync_latest_gfs(
        self,
        *,
        cleanup_old_cycles: bool = False,
        target_cycles: list[str] | None = None,
    ) -> dict[str, Any]:
        """串行检查并触发最近 00Z；实际下载由 tx-lab 的 nohup 进程持有。"""
        if cleanup_old_cycles:
            raise ValueError("预取不执行删除；请使用显式资料清理入口")
        if self.gfs is None:
            raise RuntimeError("GFS 周期管理器未配置")
        with self._gfs_sync_lock:
            selected_cycles = list(target_cycles or self.gfs.latest_cycles(
                count=self.settings.hpc_gfs_retained_cycles,
            ))
            protected_cycles = self.protected_gfs_cycles()
            auto_cleanup = {"requested": [], "deleted": [], "missing": []}
            pool = self.hpc.gfs_pool_items(selected_cycles, protected_cycles)[0]
            by_cycle = {item["cycle"]: item for item in pool.get("cycles") or []}
            actions: list[dict[str, Any]] = []
            for cycle in selected_cycles:
                item = by_cycle.get(cycle) or {}
                if item.get("complete"):
                    actions.append({"cycle": cycle, "status": "ready", "detail": f"{item.get('total_files', 73)}/{item.get('total_files', 73)}"})
                else:
                    actions.append(self.hpc.trigger_gfs_download(cycle, 72))
            # Never remove an old usable cycle until *both* replacement cycles
            # are locally complete.  This keeps automatic fallback available.
            refreshed = self.hpc.gfs_pool_items(selected_cycles, protected_cycles)[0]
            apply_forcing_observations(actions, refreshed)
            return {
                "target_cycles": selected_cycles,
                "actions": actions,
                "auto_cleanup": auto_cleanup,
            }

    def sync_latest_ecmwf(
        self,
        *,
        cleanup_old_cycles: bool = False,
        target_cycles: list[str] | None = None,
    ) -> dict[str, Any]:
        """预取最新 ECMWF 00Z 周期，规则与 GFS 共享池保持一致。"""
        if cleanup_old_cycles:
            raise ValueError("预取不执行删除；请使用显式资料清理入口")
        if self.gfs is None:
            raise RuntimeError("ECMWF 周期管理器未配置")
        with self._gfs_sync_lock:
            selected_cycles = list(target_cycles or self.gfs.latest_cycles(
                count=self.settings.hpc_gfs_retained_cycles,
                data_source="ecmwf",
            ))
            protected_cycles = self.active_forcing_cycles("ecmwf")
            auto_cleanup = {"requested": [], "deleted": [], "missing": []}
            pool = self.hpc.forcing_pool_items("ecmwf", selected_cycles, protected_cycles)[0]
            by_cycle = {item["cycle"]: item for item in pool.get("cycles") or []}
            actions: list[dict[str, Any]] = []
            for cycle in selected_cycles:
                item = by_cycle.get(cycle) or {}
                if item.get("complete"):
                    actions.append({"cycle": cycle, "status": "ready", "detail": f"{item.get('total_files', 25)}/{item.get('total_files', 25)}"})
                else:
                    actions.append(self.hpc.trigger_forcing_download("ecmwf", cycle, 72))
            refreshed = self.hpc.forcing_pool_items("ecmwf", selected_cycles, protected_cycles)[0]
            apply_forcing_observations(actions, refreshed)
            return {
                "target_cycles": selected_cycles,
                "actions": actions,
                "auto_cleanup": auto_cleanup,
            }

    def _prefetch_latest_gfs_once(self) -> None:
        policy=self.store.forcing_policy(self.settings.hpc_gfs_prefetch_enabled)
        if not isinstance(policy,dict):
            policy={"enabled":self.settings.hpc_gfs_prefetch_enabled,"auto_cleanup":False,"held_cycles":{"gfs":[],"ecmwf":[]}}
        if not policy["enabled"]:
            self.set_forcing_policy(policy)
            return
        # The remote node can be offline when 8007 starts and reconnect later
        # overnight. Refresh on every cycle instead of relying on the one-time
        # startup health thread, so a transient connection failure cannot leave
        # prefetch permanently idle.
        self._check_health()
        if self.hpc_health.get("status") != "ready":
            storage_blocked = self.hpc_health.get("status") in {"storage_unavailable", "storage_low"}
            self._set_gfs_prefetch_status(
                status="blocked_storage" if storage_blocked else "waiting_connection",
                message=(
                    self.hpc_health.get("message") or "tx-lab 数据池空间不足，本轮预取已跳过，将持续复查"
                    if storage_blocked
                    else "等待 tx-lab 连接恢复后继续后台预取"
                ),
            )
            return
        self._set_gfs_prefetch_status(status="checking", message="正在检查最新 GFS、ECMWF 00Z")
        try:
            # 只在新的受管中国区域池中保留配置数量的周期；活动任务周期始终受保护。
            result = self.sync_latest_gfs(
                cleanup_old_cycles=False,
                target_cycles=self.gfs.latest_cycles(
                    count=2,
                ),
            )
            ec_result = self.sync_latest_ecmwf(cleanup_old_cycles=False,
                target_cycles=self.gfs.latest_cycles(count=2,data_source="ecmwf"))
            if policy["auto_cleanup"]:
                for source in ("gfs","ecmwf"):
                    targets=self.gfs.latest_cycles(count=2,data_source=source)
                    expanded = getattr(self.store,"retention_approved",lambda _source:False)(source) is True
                    if expanded:
                        self.refresh_forcing_terminal_proofs()
                        self.hpc.expire_forcing_prefetch(source,targets)
                    protected=self.active_forcing_cycles(source)|set(policy["held_cycles"][source])
                    pool=self.hpc.forcing_pool_items(source,targets,protected)[0]
                    # Keep both most recent complete cycles and all task/hold references.
                    if expanded or (pool.get("targets_complete") and len(pool.get("retained_complete_cycles") or [])>=2):
                        key = "cleanup_candidates" if expanded else "auto_cleanup_candidates"
                        paths=list(pool.get(key) or [])
                        if paths:
                            service=getattr(self,"forcing_cleanup_service",None)
                            if service is None:
                                self.cleanup_forcing(source,paths)
                            else:
                                for path in paths:
                                    service.delete_cycle_async(source,Path(path).name,"system")
        except Exception as exc:
            self._set_gfs_prefetch_status(status="error", message=str(exc))
            return
        gfs_actions = result.get("actions") or []
        ec_actions = ec_result.get("actions") or []
        actions = [
            {"data_source": "gfs", **action} for action in gfs_actions
        ] + [
            {"data_source": "ecmwf", **action} for action in ec_actions
        ]
        statuses = {str(item.get("status") or "") for item in actions}
        if actions and statuses == {"ready"}:
            status = "ready"
            message = "最新两个 GFS、ECMWF 00Z 周期已就绪"
        elif statuses & {"started", "running", "downloading"}:
            status = "downloading"
            message = "tx-lab 正在后台下载 GFS/ECMWF；完成后会继续检查新周期"
        elif statuses & {"queued"}:
            status = "queued"
            message = "资料已排队，等待下载工作器或共享传输槽"
        elif statuses & {"rate_limited"}:
            status = "rate_limited"
            message = "上游限流，等待重试窗口"
        elif statuses & {"waiting_publish"}:
            status = "waiting_publish"
            message = "等待上游完整周期发布确认"
        elif statuses & {"retry_wait"}:
            status = "retry_wait"
            message = "GFS/ECMWF 暂时不可用，已进入退避重试"
        elif statuses & {"failed", "error"}:
            status = "error"
            message = "GFS/ECMWF 后台下载启动失败，将自动重试"
        else:
            status = "checked"
            message = "已完成 GFS 后台检查"
        self._set_gfs_prefetch_status(
            status=status,
            message=message,
            target_cycles=result.get("target_cycles") or [],
            ecmwf_target_cycles=ec_result.get("target_cycles") or [],
            actions=actions,
            auto_cleanup={
                "gfs": result.get("auto_cleanup") or {},
                "ecmwf": ec_result.get("auto_cleanup") or {},
            },
        )

    def _gfs_prefetch_loop(self) -> None:
        if self._stop.wait(self.settings.hpc_gfs_prefetch_start_delay_seconds):
            return
        while not self._stop.is_set():
            self._reconcile_forcing_downloads()
            self._prefetch_latest_gfs_once()
            self._forcing_cleanup_wakeup.wait(self.settings.hpc_gfs_prefetch_interval_seconds)
            self._forcing_cleanup_wakeup.clear()

    def _reconcile_forcing_downloads(self):
        for operation in self.store.list_forcing_operations(limit=100):
            if self._stop.is_set():
                return
            if operation["status"] not in {"accepted","unknown"}:
                continue
            try:
                observed = self.hpc.inspect_forcing_files(operation["source"],operation["cycle"],operation["detail"]["hours"])
                if observed.get("complete"):
                    self.store.update_forcing_operation(operation["id"],"ready",{"missing_hours":[]})
                    self._forcing_cleanup_wakeup.set()
            except Exception:
                # Preserve uncertain references; this loop must never enqueue.
                continue

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
        self.store.recover_interrupted_operations()
        threading.Thread(target=self._check_health, daemon=True, name="wrf-hpc-health").start()
        if self.settings.ordinary_max_concurrent > 1:
            self._resource_dispatch_thread = threading.Thread(target=self._resource_dispatch_loop, daemon=True, name='wrf-resource-dispatch')
            self._resource_dispatch_thread.start()
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
        if not (
            self._gfs_prefetch_thread and self._gfs_prefetch_thread.is_alive()
        ):
            self._gfs_prefetch_thread = threading.Thread(
                target=self._gfs_prefetch_loop,
                daemon=True,
                name="wrf-gfs-prefetch",
            )
            self._gfs_prefetch_thread.start()
        for task in pending_cancellations:
            if (task.get("runtime") or {}).get("remote_pid") or (task.get("runtime") or {}).get("remote_launch_attempted"):
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
        self._forcing_cleanup_wakeup.set()
        with self._state_lock:
            task_threads = list(self._task_threads.values())
            if self._resource_dispatch_thread: task_threads.append(self._resource_dispatch_thread)
            task_threads.extend(self._cancel_observers)
            prefetch_thread = self._gfs_prefetch_thread
            cancel_threads = list(self._cancel_threads.values())
            reconcile_timers = list(self._reconcile_timers.values())
            self._reconcile_timers.clear()
            reconcile_timers.extend(self._cancel_timers.values())
            self._cancel_timers.clear()
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
            self._pending_task_ids.add(task_id)
            task = self.store.get(task_id)
            if not task or task["status"] in {"succeeded", "failed", "cancelled"}:
                self._pending_task_ids.discard(task_id)
                return False
            # Unknown remote attempts retain their resource reservation across
            # API restarts. Reconciliation can run; a new computation cannot.
            if task["status"] == "queued":
                if (task.get('runtime') or {}).get('resource_retry_at', 0) > time.time(): return False
                occupied = set(self._task_threads)
                for existing_task in self.store.with_status("running", "rendering", "reconciling", "cancel_pending", "paused_external", "waiting_restart"):
                    runtime = existing_task.get("runtime") or {}
                    if runtime.get("remote_launch_attempted") and not runtime.get("remote_wrf_succeeded") and runtime.get("remote_terminal_status") not in {"failed", "cancelled", "succeeded"}:
                        occupied.add(existing_task["id"])
                occupied.discard(task_id)
                if len(occupied) >= self.settings.ordinary_max_concurrent:
                    self._merge_runtime(task_id, resource_wait_reason='计算名额已占用（最多4个任务）' if self.settings.ordinary_max_concurrent > 1 else '等待执行名额')
                    return False
                if self.settings.ordinary_max_concurrent > 1 and isinstance(self.hpc, HpcClient):
                    from runtime_release import compute_budget
                    other = [self.store.get(identity) for identity in occupied]
                    other = [item for item in other if item]
                    def legacy_supervisor(item):
                        runtime = item.get('runtime') or {}
                        # An explicit immutable supervisor binding is only
                        # created by the shared resource-pool launch path.
                        return bool(runtime.get('release_id') and not (runtime.get('supervisor_release_id') and runtime.get('supervisor_fingerprint')))
                    legacy = legacy_supervisor(task)
                    if (legacy and occupied) or any(legacy_supervisor(item) for item in other):
                        self._merge_runtime(task_id, resource_wait_reason='原发布任务保持串行，等待其资源释放')
                        return False
                    budget = compute_budget(task['request'], self.settings.hpc_cpu_mpi_processes)
                    budgets = [(item.get('runtime') or {}).get('resource_budget') or compute_budget(item['request'], self.settings.hpc_cpu_mpi_processes) for item in other]
                    cpu = int(self._health.get('cpu_count') or 0) - self.settings.compute_cpu_headroom
                    memory = int(self._health.get('memory_available_mb') or 0) * 1024**2 - self.settings.compute_memory_headroom_bytes
                    reason = ('等待有效资源观测' if self._health.get('status') != 'ready' else
                              'CPU预留不足' if sum(item['mpi_processes'] for item in budgets)+budget['mpi_processes'] > cpu else
                              '可用内存不足，等待预留释放' if sum(item['memory_bytes'] for item in budgets)+budget['memory_bytes'] > memory else '')
                    if reason:
                        self._merge_runtime(task_id, resource_wait_reason=reason)
                        return False
                    self._merge_runtime(task_id, resource_budget=budget, resource_wait_reason=None)
            elif task["status"] == "reconciling" and task_id in self._reconcile_timers:
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

    def _dispatch_pending(self) -> None:
        for task in sorted(self.store.with_status("queued", "reconciling"), key=lambda item: (item.get('created_at',''), item['id'])):
            self._dispatch(task["id"])

    @staticmethod
    def new_task_id(data_source: str = "gfs", simulation_start: Any | None = None) -> str:
        source = str(data_source or "gfs").lower()
        if source not in {"gfs", "ecmwf"}:
            raise ValueError("数据源必须是 gfs 或 ecmwf")
        if isinstance(simulation_start, str):
            simulation_start = datetime.fromisoformat(simulation_start.replace("Z", "+00:00"))
        if not isinstance(simulation_start, datetime):
            raise ValueError("任务 ID 需要有效的模拟起报时间")
        stamp = simulation_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"wrf_{source}_{stamp}_{uuid.uuid4().hex[:8]}"

    def submit(self, request: dict[str, Any], owner_sub: str = "") -> dict[str, Any]:
        request = normalize_task_request(request)
        data_source = str(request.get("data_source") or "gfs").lower()
        task_id = self.new_task_id(data_source, request["start_time"])
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
            release_id=self.settings.hpc_runtime_release,
        )
        self._dispatch(task_id)
        self._log(task_id, "任务已创建，进入持久化队列")
        return task

    def submit_idempotent(self, request: dict[str, Any], owner_sub: str, key: str) -> dict[str, Any]:
        """Single-worker submission gate; durable reservation survives request timeouts/restarts."""
        request = normalize_task_request(request)
        source = str(request.get("data_source") or "gfs").lower()
        simulation_start = request["start_time"]
        if isinstance(simulation_start, str):
            simulation_start = datetime.fromisoformat(simulation_start.replace("Z", "+00:00"))
        stamp = simulation_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        task_id = f"wrf_{source}_{stamp}_" + hashlib.sha256(f"{owner_sub}:{key}".encode()).hexdigest()[:8]
        with self._state_lock:
            reservation = self.store.reserve_submission(owner_sub, key, request, task_id)
            existing = self.store.get(task_id)
            if existing:
                self.store.accept_submission(owner_sub, key)
                if existing["status"] == "queued" and task_id not in self._task_threads:
                    self._dispatch(task_id)
                return existing
            if reservation["accepted"]:
                raise ValueError("该提交对应任务已删除，不能复用原确认重新计算")
            task_dir = self.settings.run_dir / task_id
            (task_dir / "raw").mkdir(parents=True, exist_ok=True)
            (task_dir / "task.request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
            self.store.create(task_id, request, owner_sub)
            self.store.accept_submission(owner_sub, key)
            task = self._merge_runtime(task_id,
                attempt_started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                requested_runtime_profile=FIXED_RUNTIME_PROFILE, active_runtime_profile=FIXED_RUNTIME_PROFILE,
                fallback_used=False, release_id=self.settings.hpc_runtime_release)
            self._dispatch(task_id)
            self._log(task_id, "任务已创建，进入持久化队列")
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
        archived = attempts(task_id)
        current = self.store.get(task_id)
        records = ([current] if current else []) + [item for item in archived
            if not current or item["attempt_no"] != current.get("attempt_no", 1)]
        return [{key: item.get(key) for key in (
            "attempt_no", "status", "stage", "progress", "failure", "error",
            "started_at", "finished_at", "request")} for item in records]

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

    def task_forcing_reference_counts(self, data_source: str, release_terminal=False) -> dict[str, int]:
        source = str(data_source or "gfs").lower()
        tasks = list(self.store.active())
        # A failed/paused attempt may still be recovered. Its forcing is not
        # disposable merely because no model process is currently running.
        with_status = getattr(self.store, "with_status", lambda *_args: [])
        for status in ("paused_external", "waiting_restart", "failed", "partial_success", "cancelled"):
            tasks.extend(with_status(status))
        references: dict[str, set[str]] = {}
        for index, task in enumerate(tasks):
            if (release_terminal or (getattr(self.store,"retention_approved",lambda _source:False)(source) is True)) and self._forcing_terminal_released(task):
                continue
            request = task.get("request") or {}
            if str(request.get("data_source") or "gfs") != source:
                continue
            cycle = self._forcing_cycle(task.get("runtime") or {})
            candidates = {str(cycle)} if cycle else set()
            if not cycle:
                # A queued task has not selected its pool cycle yet. Protect
                # every 00Z cycle that could cover its window until binding.
                try:
                    start = datetime.fromisoformat(request["start_time"].replace("Z", "+00:00"))
                    start -= timedelta(hours=resolve_spinup_hours(request))
                    end = datetime.fromisoformat(request["end_time"].replace("Z", "+00:00"))
                    interval = int(request.get("forecast_interval_hours") or (3 if source == "ecmwf" else 1))
                    buffer = boundary_buffer_hours(request.get("configuration_profile"))
                    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
                    manager = GfsManager(self.settings)
                    for days in range(4):
                        key = manager.cycle_key(cursor-timedelta(days=days))
                        try:
                            manager.required_hours(start,end,interval,key,data_source=source,buffer_hours=buffer)
                        except ValueError:
                            continue
                        candidates.add(key)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("任务资料引用尚无法确定，禁止清理") from exc
                if not candidates:
                    raise ValueError("任务资料窗口尚无法确定，禁止清理")
            identity = str(task.get("id") or index)
            for candidate in candidates:
                references.setdefault(candidate,set()).add(identity)
        return {cycle:len(owners) for cycle,owners in references.items()}

    def _forcing_terminal_released(self, task):
        from retirement import sha
        if task.get("status") not in {"failed","waiting_restart","partial_success","cancelled"}:
            return False
        cache = getattr(self,"_forcing_terminal_proofs",{}).get(task["id"])
        return bool(cache and cache[0] == sha(task) and time.monotonic()-cache[1] < 300)

    def refresh_forcing_terminal_proofs(self, client=None):
        from terminal_evidence import verify_forcing_terminal
        from retirement import sha
        if not hasattr(self,"_forcing_terminal_proofs"):
            self._forcing_terminal_proofs = {}
        for state in ("failed","waiting_restart","partial_success","cancelled"):
            for task in self.store.with_status(state):
                self._forcing_terminal_proofs.pop(task["id"],None)
                try:
                    proof = verify_forcing_terminal(self.settings,client or self.hpc,task)
                    if proof.get("terminal") is True:
                        self._forcing_terminal_proofs[task["id"]] = (sha(task),time.monotonic())
                except Exception:
                    # An unknown consumer always keeps its reference.
                    continue

    def active_forcing_cycles(self, data_source: str) -> set[str]:
        return set(self.task_forcing_reference_counts(data_source)) | self.store.forcing_download_cycles(data_source)

    def active_gfs_cycles(self) -> set[str]:
        return self.active_forcing_cycles("gfs")

    def cleanup_forcing(self, source: str, paths: list[str], actor_sub: str = "system", expected_snapshots=None, _reference_guard_held=False, retention_upgrade=False, _client=None) -> dict[str, Any]:
        client=_client or self.hpc
        extra={"operation_timeout":getattr(client,"hash_timeout",120)} if _client is not None else {}
        if not self.settings.hpc_workbench_data_root or not str(getattr(self.settings, "hpc_gfs_dir" if source == "gfs" else "hpc_ec_dir")).startswith(self.settings.hpc_workbench_data_root.rstrip("/")+"/forcing/"):
            raise ValueError("资料清理只允许独立普通 WRF 资料池")
        from uuid import uuid4
        audit_id = "cleanup_" + uuid4().hex
        self.store.record_forcing_cleanup(audit_id, actor_sub, source, paths, "checking")
        try:
            with nullcontext() if _reference_guard_held else forcing_reference_guard(self.settings.database_path):
                expanded = retention_upgrade or getattr(self.store,"retention_approved",lambda _source:False)(source) is True
                if expanded:
                    self.refresh_forcing_terminal_proofs(client) if _client is not None else self.refresh_forcing_terminal_proofs()
                targets = self.gfs.latest_cycles(count=self.settings.hpc_gfs_retained_cycles, data_source=source)
                policy = self.store.forcing_policy(self.settings.hpc_gfs_prefetch_enabled)
                holds = set(policy.get("held_cycles", {}).get(source, [])) if isinstance(policy, dict) else set()
                retained = set(targets) if isinstance(targets, (list, tuple, set)) else set()
                references = set(self.task_forcing_reference_counts(source,release_terminal=expanded)) | self.store.forcing_download_cycles(source)
                protection = references | holds | retained
                if expected_snapshots is None:
                    result = client.cleanup_forcing_cycles(source, paths, targets, protection,**extra)
                else:
                    result = client.cleanup_forcing_cycles(source, paths, targets, protection,expected_snapshots=expected_snapshots,**extra)
            self.store.update_forcing_cleanup(audit_id, "complete", result if isinstance(result, dict) else {})
            return {**result, "audit_id": audit_id} if isinstance(result, dict) else result
        except Exception as exc:
            self.store.update_forcing_cleanup(audit_id, "unknown", {"error_type": type(exc).__name__})
            raise

    def _forcing_wait_deadline(self, task_id: str) -> float:
        runtime = self.get(task_id).get("runtime") or {}
        deadline = runtime.get("forcing_wait_deadline")
        if deadline is None:
            deadline = time.time() + self.settings.hpc_forcing_wait_timeout_seconds
            self._merge_runtime(task_id, forcing_wait_deadline=deadline)
        return float(deadline)

    def retained_gfs_cycles(self) -> set[str]:
        if self.gfs is None:
            return set()
        return set(self.gfs.latest_cycles(count=self.settings.hpc_gfs_retained_cycles))

    def protected_gfs_cycles(self) -> set[str]:
        return self.active_gfs_cycles() | self.retained_gfs_cycles()

    def _select_pool_cycle(
        self,
        data_source: str,
        model_start: datetime,
        end: datetime,
        interval: int,
        requested_cycle: str,
        *,
        buffer_hours: int = 6,
    ) -> tuple[str, list[int]]:
        """Select the newest complete local cycle that covers this task.

        The nominal cycle remains the demand target when no complete fallback
        exists, allowing the source queue to download only the task's fields.
        """
        requested_hours = self.gfs.required_hours(
            model_start, end, interval, requested_cycle, data_source=data_source, buffer_hours=buffer_hours,
        )
        complete_cycles = getattr(self.hpc, "complete_forcing_cycles", None)
        if complete_cycles:
            for candidate in complete_cycles(data_source):
                try:
                    hours = self.gfs.required_hours(
                        model_start, end, interval, candidate, data_source=data_source, buffer_hours=buffer_hours,
                    )
                except ValueError:
                    continue
                return candidate, hours
        return requested_cycle, requested_hours

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

    def operations(self, task_id: str) -> list[dict[str, Any]]:
        """Describe local eligibility; remote proof is required when planning."""
        task = self.get(task_id)
        from recovery import fingerprint
        revision = fingerprint(task)
        runtime = task.get("runtime") or {}
        status = task["status"]
        completed = bool(runtime.get("remote_wrf_succeeded"))
        blocked = self.store.deletion_blocked(task_id)
        conditions = {
            "cancel": (status not in FINAL_STATES, "当前任务已结束"),
            "resume": (status in {"paused_external", "reconciling"} or
                       (status == "failed" and (task.get("failure") or {}).get("failure_class") == "external"),
                       "当前任务不是可恢复的连接中断"),
            "retry_outputs": (status == "failed" and completed and bool(self._forcing_cycle(runtime)),
                              "需要已完成的远端计算及资料周期记录"),
            "render_partial": (status == "failed" and completed and bool(self._forcing_cycle(runtime)) and
                               bool((runtime.get("output_validation") or {}).get("invalid") or any(
                                   word in str(task.get("error") or "").lower()
                                   for word in ("netcdf", "hdf", "wrfout", "unknown file format"))),
                               "需要已完成的计算及已确认的不可读帧"),
            "restart": (status in {"waiting_restart", "failed", "cancelled"} and not completed,
                        "仅失败或已取消的任务可以修改后再次启动"),
            "publish": (task["request"].get("publish_to_catalog", True) is not False
                        and status == "succeeded" and isinstance((task.get("result") or {}).get("meta_json"), dict)
                        and ((task.get("result") or {}).get("catalog_publication") or {}).get("status") != "success",
                        "仅完整成功且尚未成功登记的结果可以重试发布"),
        }
        return [{"action": action, "enabled": bool(enabled and not blocked),
                 "reason": "任务删除事务进行中" if blocked else (None if enabled else reason),
                 "requires_confirmation": True, "state_fingerprint": revision,
                 "remote_check_required": action == "restart"}
                for action, (enabled, reason) in conditions.items()]

    def require_operation(self, task_id: str, action: str, *, allow_completed: bool = False) -> None:
        operation = next((item for item in self.operations(task_id) if item["action"] == action), None)
        if allow_completed and action == "restart":
            task = self.get(task_id)
            if (task["status"] == "failed" and (task.get("runtime") or {}).get("remote_wrf_succeeded")
                    and not self.store.deletion_blocked(task_id)):
                self._require_recovery_idle(task_id)
                return
        if not operation or not operation["enabled"]:
            raise TaskConflictError(operation["reason"] if operation else "未知任务操作")
        self._require_recovery_idle(task_id)

    def restart_plan(self, task_id: str, *, allow_completed: bool = False) -> dict[str, Any]:
        task = self.get(task_id)
        if task["status"] not in {"waiting_restart", "failed", "cancelled"}:
            raise TaskConflictError("只有已失败、待调整或已取消的任务可以清理后重跑")
        runtime = task.get("runtime") or {}
        if runtime.get("remote_wrf_succeeded") and not allow_completed:
            raise TaskConflictError("远端 WRF 已完成，请恢复结果下载，不要清理重跑")
        if allow_completed and runtime.get("remote_wrf_succeeded"):
            if not runtime.get("release_id"):
                raise TaskConflictError("旧计算缺少独立尝试身份；保留输出，禁止原任务重新计算")
            from terminal_evidence import verify_forcing_terminal
            proof = verify_forcing_terminal(self.settings,self.hpc,task)
            status = {"status":"succeeded" if proof.get("terminal") is True else "unknown"}
        else:
            status = None
        prover = self.hpc.terminal_evidence if callable(getattr(type(self.hpc), "terminal_evidence", None)) else None
        if status is not None:
            pass
        elif task["status"] in {"waiting_restart", "failed", "cancelled"} and callable(prover):
            proof = prover(task)
            status = {"status": "failed" if proof.get("terminal") is True else "unknown"}
        else:
            status = self.hpc.status(task_id)
        remote_status = str(status.get("status") or "unknown")
        allow_missing = not runtime.get("remote_pid") and not runtime.get("remote_launch_attempted")
        can_restart = remote_status == "failed" or (remote_status == "missing" and allow_missing) or (remote_status == "succeeded" and allow_completed)
        reason = None
        if remote_status == "running":
            reason = "远端任务仍在运行"
        elif remote_status == "succeeded" and not allow_completed:
            reason = "远端任务已成功，应恢复结果下载"
        elif remote_status == "lost":
            reason = "远端进程状态不明"
        elif remote_status == "missing" and not allow_missing:
            reason = "任务曾启动远端进程，但远端记录缺失"
        remote_paths = [] if runtime.get("release_id") else getattr(self.hpc, "task_artifact_paths", lambda _task_id: [])(task_id)
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
        archived_log = attempt_dir / "service.log"
        sources = log_paths(self.settings, task)
        if sources:
            with archived_log.open("wb") as output:
                for source in sources:
                    with source.open("rb") as stream:
                        shutil.copyfileobj(stream, output)
        snapshot = {
            key: task.get(key)
            for key in ("id", "attempt_no", "status", "stage", "progress", "request", "runtime", "result", "failure", "error")
        }
        (attempt_dir / "attempt.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return archived_log

    @serialized_task_operation
    def restart(
        self,
        task_id: str,
        request: dict[str, Any],
        *,
        confirm_task_id: str,
        confirm_attempt: int,
        allow_completed: bool = False,
    ) -> dict[str, Any]:
        request = normalize_task_request(request)
        task = self.get(task_id)
        if confirm_task_id != task_id or confirm_attempt != int(task.get("attempt_no") or 1):
            raise ValueError("任务或尝试次数确认值已过期，请重新获取清理清单")
        self._require_recovery_idle(task_id)
        plan = self.restart_plan(task_id,**({"allow_completed":True} if allow_completed else {}))
        if not plan["can_restart"]:
            raise TaskConflictError(plan.get("reason") or "当前远端状态不允许清理重跑")
        if (task.get("failure") or {}).get("derived"):
            persisted_failure = dict(task["failure"])
            persisted_failure.pop("derived", None)
            self.store.update(task_id, failure=persisted_failure)
            task["failure"] = persisted_failure
        if allow_completed and (task.get("runtime") or {}).get("remote_wrf_succeeded"):
            destination = self.settings.run_dir / task_id / "attempts" / f"{int(task.get('attempt_no') or 1):03d}"
            for name in ("raw","products"):
                if (destination / name).exists():
                    raise TaskConflictError("旧输出归档目标已存在；未覆盖")
        archived_log = self._archive_attempt_files(task)
        archive_attempt = getattr(self.store, "archive_attempt", None)
        if archive_attempt:
            archive_attempt(task_id, str(archived_log) if archived_log.exists() else None)
        runtime = task.get("runtime") or {}
        cleanup = getattr(self.hpc, "cleanup_task_attempt", None)
        preserve_output = allow_completed and runtime.get("remote_wrf_succeeded")
        if cleanup and not preserve_output:
            cleanup(
                task_id,
                allow_missing=not runtime.get("remote_pid") and not runtime.get("remote_launch_attempted"),
            )
        for path in self._restart_local_paths(task_id):
            output_root = (self.settings.output_dir / "runs" / task_id).resolve()
            if preserve_output and (path.name == "raw" or path == output_root) and path.exists():
                destination = archived_log.parent / ("products" if path == output_root else "raw")
                if destination.exists() or path.is_symlink():
                    raise TaskConflictError("旧输出归档目标已存在或路径异常；未覆盖")
                path.rename(destination)
                continue
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
        with forcing_reference_guard(self.settings.database_path):
            restarted = begin_attempt(task_id, request)
            self._forcing_terminal_proofs.pop(task_id,None)
        self._forcing_cleanup_wakeup.set()
        self._dispatch(task_id)
        self._log(task_id, f"已清理任务级残片并开始第 {restarted['attempt_no']} 次尝试；共享 GFS 数据池已保留")
        return self.get(task_id)

    @guard_legacy_action
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

    @guard_legacy_action
    def retry_outputs(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task["status"] != "failed":
            raise TaskConflictError("只有失败任务可以重试结果下载")
        runtime = dict(task.get("runtime") or {})
        if not runtime.get("remote_wrf_succeeded") or not self._forcing_cycle(runtime):
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

    @guard_legacy_action
    def render_partial(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        runtime = dict(task.get("runtime") or {})
        if task["status"] != "failed":
            raise TaskConflictError("只有结果完整性校验失败的任务可以部分渲染")
        if not runtime.get("remote_wrf_succeeded") or not self._forcing_cycle(runtime):
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

    @serialized_task_operation
    def retry_catalog_publish(self, task_id: str) -> dict[str, Any]:
        with self._operation_lock(task_id):
            return self._retry_catalog_publish(task_id)

    def _retry_catalog_publish(self, task_id: str) -> dict[str, Any]:
        self._require_recovery_idle(task_id)
        task = self.get(task_id)
        if ((task.get("result") or {}).get("catalog_publication") or {}).get("status") == "success":
            return task
        if task["status"] != "succeeded" or not task.get("result"):
            raise TaskConflictError("只有完整成功的 WRF 任务可以重试统一目录发布")
        manifest = (task.get("result") or {}).get("meta_json")
        if not isinstance(manifest, dict):
            raise TaskConflictError("任务缺少可发布的 scene.meta.json")
        publication = self._publish_catalog_result(task_id, task, manifest)
        runtime = dict(task.get("runtime") or {})
        runtime["catalog_publication"] = publication
        result = dict(task.get("result") or {})
        result["catalog_publication"] = publication
        return self.store.update(task_id, runtime=runtime, result=result)

    @staticmethod
    def _managed_task_path(root: Path, task_id: str) -> Path:
        base = root.resolve()
        candidate = (base / task_id).resolve()
        if Path(task_id).name != task_id or candidate.parent != base:
            raise ValueError("任务目录不在受管路径内")
        return candidate

    def delete_local(self, task_id: str, confirm_task_id: str) -> dict[str, Any]:
        raise TaskConflictError("旧本地删除入口已停用；请使用精确删除计划")

    def _log_path(self, task_id: str) -> Path:
        return self.settings.run_dir / task_id / "service.log"

    def _log(self, task_id: str, message: str) -> None:
        path = self._log_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with path.open("a", encoding="utf-8") as output:
            output.write(f"[{stamp}] {message.rstrip()}\n")

    def diagnostic_evidence(self, task_id: str, attempt_no: int) -> dict[str, Any]:
        """Read only this attempt's bounded logs, without changing task state."""
        task = self.get(task_id)
        current = int(task.get("attempt_no") or 1)
        paths = log_paths(self.settings, task)
        if attempt_no != current:
            archived = next((item for item in self.store.attempts(task_id)
                             if int(item["attempt_no"]) == attempt_no), None)
            if not archived or not archived.get("log_path"):
                return {"evidence": [], "sources": [{"source": "archived_log", "status": "missing"}]}
            paths = [archived_log_path(self.settings, task, attempt_no, archived["log_path"])]
        sources, evidence = [], []
        try:
            if paths:
                evidence.append(read_log_bytes(paths, limit=24000, tail=True)[1].decode("utf-8", errors="replace"))
            sources.append({"source": "local_log", "status": "available" if paths else "missing"})
        except FileNotFoundError:
            sources.append({"source": "local_log", "status": "missing"})
        except OSError:
            sources.append({"source": "local_log", "status": "unavailable"})
        remote_status = "not_checked"
        if attempt_no == current:
            try:
                observation = self.hpc.diagnostic_status(task_id)
                remote_status = str(observation.get("status") or "unknown")
                prover = self.hpc.terminal_evidence if callable(getattr(type(self.hpc), "terminal_evidence", None)) else None
                if task["status"] == "waiting_restart" and callable(prover):
                    proof = prover(task)
                    if proof.get("terminal") is True:
                        remote_status = "failed"
                        sources.append({"source": "terminal_" + proof["mode"], "status": "available"})
                if observation.get("log"):
                    evidence.append(str(observation["log"])[-24000:])
                sources.append({"source": "remote_log", "status": "available" if observation.get("log") else "missing"})
            except (RuntimeError, ValueError, OSError, TimeoutError):
                remote_status = "unavailable"
                sources.append({"source": "remote_log", "status": "unavailable"})
        return {"evidence": evidence, "sources": sources, "remote_status": remote_status}

    def logs(
        self,
        task_id: str,
        after: int = 0,
        limit: int = 65536,
        attempt_no: int | None = None,
    ) -> dict[str, Any]:
        task = self.get(task_id)
        current_attempt = int(task.get("attempt_no") or 1)
        paths = log_paths(self.settings, task)
        if attempt_no is not None and attempt_no != current_attempt:
            attempts = getattr(self.store, "attempts", lambda _task_id: [])(task_id)
            archived = next(
                (item for item in attempts if int(item.get("attempt_no") or 0) == attempt_no),
                None,
            )
            if not archived or not archived.get("log_path"):
                raise TaskNotFoundError(f"{task_id}/attempt/{attempt_no}")
            paths = [archived_log_path(self.settings, task, attempt_no, archived["log_path"])]
        if not paths or not all(path.is_file() for path in paths):
            return {"offset": 0, "text": ""}
        offset, data = read_log_bytes(paths, after, limit)
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text)
        text = "".join(char for char in text if char in "\n\r\t" or ord(char) >= 32)
        return {"offset": offset, "text": text}

    def _merge_runtime(self, task_id: str, **values: Any) -> dict[str, Any]:
        with self._state_lock:
            task = self.get(task_id)
            runtime = dict(task.get("runtime") or {})
            runtime.update(values)
            return self.store.update(task_id, runtime=runtime)

    def _publish_catalog_result(
        self,
        task_id: str,
        task: dict[str, Any],
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        if task["request"].get("publish_to_catalog", True) is False:
            return {"status": "disabled", "reason": "task_publication_disabled"}
        if self.store.deletion_blocked(task_id):
            raise TaskConflictError("任务删除事务已开始，禁止发布")
        if not self.settings.catalog_publish_enabled:
            return {"status": "disabled"}
        source_dir = self.settings.output_dir / "runs" / task_id
        try:
            return publish_workbench_result(
                task_id,
                str(task.get("owner_sub") or ""),
                source_dir,
                manifest,
                self.settings.catalog_data_root,
            )
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "retryable": True}

    @staticmethod
    def _is_external_error(exc: Exception) -> bool:
        if isinstance(exc, RemoteTaskFailure):
            return False
        if isinstance(exc, HpcError):
            message = str(exc).lower()
            return any(marker in message for marker in EXTERNAL_ERROR_MARKERS)
        return False

    def _failure_info(self, task_id: str, exc: Exception) -> dict[str, Any]:
        from scripts.namelist_layout import is_namelist_read_failure
        task = self.get(task_id)
        runtime = task.get("runtime") or {}
        message = str(exc)
        lowered = message.lower()
        remote_failure = exc.remote_status.get("failure") if isinstance(exc, RemoteTaskFailure) else None
        stage = str((remote_failure or {}).get("stage") or task.get("stage") or "unknown")
        evidence = str(getattr(exc, "remote_status", {}).get("log") or "")[-12000:]
        if self._is_external_error(exc):
            failure_class, action, recoverable = "external", "resume", True
        elif runtime.get("remote_wrf_succeeded") or stage in {"downloading_outputs", "rendering", "checking_remote_outputs"}:
            failure_class, action, recoverable = "output", "retry_outputs", True
        elif stage in {"selecting_cycle", "checking_hpc_gfs", "waiting_for_hpc_gfs", "remote_gfs_ready", "data",
                       "checking_hpc_forcing", "waiting_for_hpc_forcing", "waiting_for_forcing_cache", "remote_forcing_ready"}:
            failure_class, action, recoverable = "data", "restart", True
        elif is_namelist_read_failure(message + "\n" + evidence):
            failure_class, action, recoverable = "configuration_generation", "regenerate_plan", True
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
            "evidence": evidence,
        }

    def _schedule_reconcile(self, task_id: str) -> None:
        delay = self.settings.hpc_reconcile_interval_seconds

        def enqueue() -> None:
            with self._state_lock:
                if self._reconcile_timers.get(task_id) is not timer:
                    return
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
        if task["request"].get("recovery_policy") == "confirm":
            self.store.update(task_id, status="paused_external", stage="paused_external",
                              failure=self._failure_info(task_id, exc), error=str(exc))
            self._log(task_id, "外部环境异常；持续保留任务，等待用户确认恢复。")
            return
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

    def _record_cancel_observation(self, task_id: str, observed: dict[str, Any], binding: tuple) -> None:
        with self._operation_lock(task_id), self._state_lock:
            task = self.get(task_id)
            runtime = task.get("runtime") or {}
            if task["status"] != "cancel_pending" or (task.get("attempt_no"), runtime.get("launch_attempt")) != binding:
                return
            status = observed.get("status", "unknown")
            if status in {"cancelled", "succeeded", "failed"}:
                self.store.update(task_id, status="cancelled", stage="cancelled", error=None)
                self._merge_runtime(task_id, remote_terminal_status=status)
            else:
                self.store.update(task_id, error=f"远端取消状态尚未确认：{status}")

    def _schedule_cancel_reconcile(self, task_id: str) -> None:
        """Observe an accepted cancellation; never replay a remote mutation."""
        def observe() -> None:
            current = threading.current_thread()
            with self._state_lock:
                if self._cancel_timers.get(task_id) is not timer:
                    return
                self._cancel_timers.pop(task_id, None)
                if self._stop.is_set():
                    return
                self._cancel_observers.add(current)
            try:
                task = self.get(task_id)
                if task["status"] != "cancel_pending":
                    return
                binding = (task.get("attempt_no"), (task.get("runtime") or {}).get("launch_attempt"))
                try:
                    result = self.hpc.status(task_id)
                except Exception:
                    result = {"status": "unknown"}
                self._record_cancel_observation(task_id, result, binding)
                self._schedule_cancel_reconcile(task_id)
                self._dispatch_pending()
            finally:
                with self._state_lock:
                    self._cancel_observers.discard(current)

        with self._state_lock:
            task = self.store.get(task_id)
            if (not self._started or self._stop.is_set() or not task or
                    task["status"] != "cancel_pending" or task_id in self._cancel_timers):
                return
            timer = threading.Timer(self.settings.hpc_reconcile_interval_seconds, observe)
            timer.daemon = True
            timer.name = f"wrf-cancel-observe-{task_id[-8:]}"
            self._cancel_timers[task_id] = timer
            timer.start()

    def _remote_cancel(self, task_id: str) -> None:
        task = self.get(task_id)
        binding = (task.get("attempt_no"), (task.get("runtime") or {}).get("launch_attempt"))
        try:
            result = self.hpc.cancel(task_id)
            for _ in range(15):
                if result.get("status") in {"cancelled", "succeeded", "failed"} or self._stop.wait(2):
                    break
                result = self.hpc.status(task_id)
            self._log(task_id, f"远端取消请求：{result['status']}")
            self._record_cancel_observation(task_id, result, binding)
        except Exception as exc:
            self._log(task_id, f"远端取消尚未确认：{exc}")
            self._record_cancel_observation(task_id, {"status": "unknown"}, binding)
        finally:
            with self._state_lock:
                current = self._cancel_threads.get(task_id)
                if current is threading.current_thread():
                    self._cancel_threads.pop(task_id, None)
            self._schedule_cancel_reconcile(task_id)
            self._dispatch_pending()

    def _start_remote_cancel(self, task_id: str) -> None:
        with self._state_lock:
            existing = self._cancel_threads.get(task_id)
            if existing:
                return
            timer = self._cancel_timers.pop(task_id, None)
            if timer:
                timer.cancel()
            thread = threading.Thread(
                target=self._remote_cancel,
                args=(task_id,),
                daemon=True,
                name=f"wrf-cancel-{task_id[-8:]}",
            )
            self._cancel_threads[task_id] = thread
        thread.start()

    @serialized_task_operation
    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._state_lock:
            # Launch intent is persisted under the same lock. Re-read after
            # acquiring it so a concurrent launch cannot be cancelled locally.
            task = self.get(task_id)
            runtime = task.get("runtime") or {}
            unresolved_attempt = (runtime.get("remote_pid") or runtime.get("remote_launch_attempted")) and not (
                runtime.get("remote_wrf_succeeded") or runtime.get("remote_terminal_status") in {"failed", "cancelled", "succeeded"})
            if task["status"] in FINAL_STATES and not unresolved_attempt:
                return task
            self._cancelled.add(task_id)
            if not runtime.get("remote_pid") and not runtime.get("remote_launch_attempted"):
                self._pending_task_ids.discard(task_id)
                self._log(task_id, "任务在远端进程启动前取消")
                result = self.store.update(
                    task_id,
                    status="cancelled",
                    stage="cancelled",
                    progress=task["progress"],
                    error=None,
                )
            else:
                result = self.store.update(
                    task_id,
                    status="cancel_pending",
                    stage="cancel_pending",
                    error=None,
                )
        if result["status"] == "cancel_pending":
            self._start_remote_cancel(task_id)
        else:
            self._dispatch_pending()
        return self.get(task_id)

    def _complete_task_cancellation(self, task_id: str) -> None:
        task = self.get(task_id)
        if task["status"] in FINAL_STATES:
            return
        if (task.get("runtime") or {}).get("remote_pid") or (task.get("runtime") or {}).get("remote_launch_attempted"):
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
            self._forcing_cleanup_wakeup.set()
            self._dispatch_pending()

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
        last_progress = time.monotonic()
        last_model_time = None
        stalled_reported = False
        task = self.get(task_id)
        binding = (task.get("attempt_no"), (task.get("runtime") or {}).get("launch_attempt"))
        while not self._stop.is_set():
            self._raise_if_cancelled(task_id)
            status = self.hpc.status(task_id)
            if not self._persist_observed_progress(task_id, status, binding):
                return False
            self._merge_remote_profile(task_id, status)
            remote_log = status.get("log", "")
            model_times = re.findall(r"Timing for main: time (\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d)", remote_log)
            model_time = max(model_times) if model_times else None
            if model_time and model_time != last_model_time:
                last_model_time, last_progress, stalled_reported = model_time, time.monotonic(), False
            if not stalled_reported and time.monotonic() - last_progress > 1800:
                task = self.get(task_id)
                task["failure"] = {"code": "stalled", "failure_class": "stalled", "message": "30 分钟未观测到模拟时间推进；需核查，不代表进程已停止"}
                if hasattr(self.store, "record_incident"):
                    self.store.record_incident(task, remote_log)
                stalled_reported = True
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

    def _persist_observed_progress(self, task_id: str, status: dict[str, Any], binding: tuple) -> bool:
        with self._operation_lock(task_id), self._state_lock:
            task = self.get(task_id)
            runtime = task.get("runtime") or {}
            if (task.get("attempt_no"), runtime.get("launch_attempt")) != binding:
                return False
            if status.get("attempt") and status["attempt"] != binding[1]:
                return False
            if task.get("status") in FINAL_STATES:
                return False
            values = observed_model_progress(task, status)
            if values and any(runtime.get(key) != value for key, value in values.items()):
                self.store.update(task_id, runtime={**runtime, **values})
            return True

    def _merge_remote_profile(self, task_id: str, status: dict[str, Any]) -> None:
        if status.get("status") in {"failed", "cancelled", "succeeded"}:
            self._merge_runtime(task_id, remote_terminal_status=status["status"])
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
        if validator is None:
            raise RuntimeError("缺少输出校验器，禁止渲染及发布")
        validation = validator(task_id)
        self._merge_runtime(task_id, output_validation=validation)
        invalid_outputs = list(validation.get("invalid") or [])
        if not validation.get("complete", False) and not allow_partial:
            names = ", ".join(str(item.get("name") or "unknown") for item in invalid_outputs[:5])
            raise RuntimeError(f"wrfout 完整性校验失败：{names or '存在缺失域或时次'}")
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
            self.hpc.download_outputs(
                task_id,
                raw_dir,
                progress=download_progress,
                include_names=valid_names or None,
            )
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
            "attempt_no": int(task.get("attempt_no") or 1),
            "meta_url": f"/data/WRF/runs/{task_id}/scene.meta.json",
            "meta_json": manifest,
        }
        partial = not validation.get("complete", False) or manifest.get("quality", {}).get("status") == "partial"
        final_status = "partial_success" if partial else "succeeded"
        publication = (
            {"status": "skipped", "reason": "partial WRF results are not catalogued"}
            if partial
            else self._publish_catalog_result(task_id, task, manifest)
        )
        result["catalog_publication"] = publication
        self._merge_runtime(task_id, catalog_publication=publication)
        self.store.update(
            task_id,
            status=final_status,
            stage="done",
            progress=100,
            result=result,
            failure={"failure_class": "output", "message": "结果部分可用，请核对缺失域、时次和关键变量", "recoverable": True, "recommended_action": "retry_outputs"} if partial else None,
            error=None,
        )
        self._log(task_id, "任务部分完成，不可读帧已排除并记录质量告警" if partial else "任务完成，WebP 与 scene.meta.json 已生成")

    def _pin_runtime_release(self, task_id: str, request: dict[str, Any]) -> None:
        """Persist the immutable release before resolving any preparation paths."""
        if not isinstance(self.hpc, HpcClient):
            return
        from runtime_release import require_ready
        runtime = self.get(task_id).get("runtime") or {}
        if not runtime.get("release_id") and (runtime.get("remote_pid") or runtime.get("remote_launch_attempted")):
            raise HpcError("历史尝试未绑定普通发布版本，不能改写其运行路径")
        effective = replace(self.settings, hpc_runtime_release=runtime["release_id"]) if runtime.get("release_id") else self.settings
        ready = require_ready(self.hpc, effective, force=True, request=request)
        release, fingerprint = ready.get("release_id"), ready.get("fingerprint")
        if not release or not fingerprint:
            raise HpcError("运行环境缺少不可变发布版本或指纹")
        if ((runtime.get("release_id") and runtime["release_id"] != release) or
                (runtime.get("release_fingerprint") and runtime["release_fingerprint"] != fingerprint)):
            raise HpcError("任务发布版本指纹已改变，拒绝准备运行")
        self._merge_runtime(task_id, release_id=release, release_fingerprint=fingerprint,
                            resource_budget=ready.get("resource_budget"))
        if self.settings.ordinary_max_concurrent > 1 and not runtime.get('remote_launch_attempted'):
            supervisor = require_ready(self.hpc, self.settings, force=True)
            self._merge_runtime(task_id, supervisor_release_id=supervisor['release_id'], supervisor_fingerprint=supervisor['fingerprint'])
        budget = ready.get("resource_budget") or {}
        if budget.get("automatic_serial_fallback"):
            self._log(task_id, f"小区域自动使用 1 个 MPI 进程（配置上限 {budget['configured_mpi_processes']}）；网格范围保持不变")

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
            cycle = self._forcing_cycle(runtime)
            if not cycle:
                raise RuntimeError("恢复结果下载缺少驱动场 cycle")
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
                cycle = self._forcing_cycle(task.get("runtime") or {})
                if not cycle:
                    raise RuntimeError("恢复任务缺少驱动场 cycle")
                self._finalize(task_id, cycle)
                return
            if action == "finalize":
                self._merge_runtime(task_id, remote_wrf_succeeded=True)
                cycle = self._forcing_cycle(runtime)
                if not cycle:
                    raise RuntimeError("恢复任务缺少驱动场 cycle")
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
                self._log(task_id, f"未启动任务按配置合同使用 CPU、{resolve_spinup_hours(request)} 小时 spin-up、{request.get('vertical_levels', 88)} 垂直层")
        self._pin_runtime_release(task_id, request)
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
        forcing_buffer = boundary_buffer_hours(request.get("configuration_profile"))
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
        requested_cycle, _ = self.gfs.select_cycle(model_start, end, data_source=data_source, buffer_hours=forcing_buffer)
        self._raise_if_cancelled(task_id)
        interval = int(request.get("forecast_interval_hours") or 1)
        # Prefer the newest complete local cycle that covers the precise
        # spin-up and boundary window. The nominal latest cycle may still be
        # publishing in the upstream feed.
        with forcing_reference_guard(self.settings.database_path):
            cycle, hours = self._select_pool_cycle(
                data_source, model_start, end, interval, requested_cycle, buffer_hours=forcing_buffer,
            )
            if cycle != requested_cycle:
                self._log(
                    task_id,
                    f"{source_label} 最新周期 {requested_cycle} 尚未完整，自动回退至完整周期 {cycle}",
                )
            self._merge_runtime(
                task_id,
                data_source=data_source,
                forcing_cycle=cycle,
                **({f"{data_source}_cycle": cycle} if data_source != "gfs" else {}),
                **({"gfs_cycle": cycle} if data_source == "gfs" else {}),
                forecast_hours=hours,
                spinup_hours=spinup_hours,
                forcing_boundary_buffer_hours=forcing_buffer,
                model_start_time=model_start.isoformat().replace("+00:00", "Z"),
                forcing_cycle_fallback_from=(requested_cycle if cycle != requested_cycle else None),
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
                    deadline_epoch=self._forcing_wait_deadline(task_id),
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
        if hasattr(self.hpc, "validate_forcing_semantics"):
            from domain_preview import DomainPreviewRequest, preview_domains
            geometry = preview_domains(DomainPreviewRequest.model_validate({
                "center": request["center"], "domains": request["domains"],
                "configuration_profile": request.get("configuration_profile"),
                "projection": ((request.get("namelist") or {}).get("wps") or {}).get("geogrid") or {},
            }))
            west, south, east, north = geometry["boundaries"][0]["bbox"]
            validation = self.hpc.validate_forcing_semantics(task_id, data_source, cycle, hours, [south, west, north, east])
            self._merge_runtime(task_id, forcing_semantic_validation=validation)
        self.store.update(task_id, status="uploading", stage="preparing_hpc", progress=66)
        self._log(task_id, "上传任务配置与输入清单；运行脚本固定于不可变发布包")

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
            prepared_release = self.hpc.prepare_runtime(
                task_id,
                config_path,
                environment_path,
                expected_forcing_path,
                Path(__file__).resolve().parent / "scripts",
                progress=prepare_progress,
            )
            if isinstance(prepared_release, dict):
                self._merge_runtime(task_id, **{key: prepared_release[key] for key in ("release_id", "release_fingerprint", "remote_task_dir") if key in prepared_release})
        finally:
            sync_transfer_status()

        with self._state_lock:
            self._raise_if_cancelled(task_id)
            self.store.update(task_id, status="running", stage="running", progress=68)
            current_runtime = self.get(task_id).get('runtime') or {}
            launch_attempt = current_runtime.get('launch_attempt') if current_runtime.get('resource_denied_before_launch') else uuid.uuid4().hex
            self._merge_runtime(task_id, remote_launch_attempted=True, launch_attempt=launch_attempt)
        launch = self.hpc.launch(task_id, data_source, attempt=launch_attempt) if hasattr(self.hpc, "inspect_forcing_files") else self.hpc.launch(task_id)
        if launch.get('resource_wait') is True:
            with self._state_lock:
                self._merge_runtime(task_id, remote_launch_attempted=False,
                                    resource_denied_before_launch=True,
                                    resource_wait_reason=launch.get('resource_wait_reason'), resource_retry_at=time.time()+10)
                if self._is_cancelled(task_id):
                    self.store.update(task_id, status='cancelled', stage='cancelled')
                else:
                    self.store.update(task_id, status='queued', stage='queued', progress=0)
            return
        self._merge_runtime(task_id, resource_denied_before_launch=False, resource_retry_at=0, resource_wait_reason=None)
        self._merge_runtime(task_id, **launch)
        self._log(task_id, f"受管启动状态：{launch.get('remote_launch_status', 'running')}")
        if self._is_cancelled(task_id):
            self.store.update(task_id, status="cancel_pending", stage="cancel_pending")
            self._start_remote_cancel(task_id)
            raise TaskCancelledError("任务在远端进程启动时收到取消请求")
        if not self._monitor(task_id):
            return
        self._merge_runtime(task_id, remote_wrf_succeeded=True)
        self._finalize(task_id, cycle)
