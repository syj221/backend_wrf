from __future__ import annotations

import json
import queue
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
from hpc import HpcClient, write_task_bundle
from renderer import render_run


FINAL_STATES = {"succeeded", "partial_success", "failed", "cancelled"}


def resolve_spinup_hours(request: dict[str, Any]) -> int:
    spinup = request.get("spinup") or {"mode": "off", "hours": 0}
    mode = spinup.get("mode", "off")
    if mode == "off":
        return 0
    if mode == "custom":
        return int(spinup.get("hours") or 0)
    focus = request.get("forecast_focus", "general")
    finest_dx = min(int(item.get("dx") or 999999) for item in request.get("domains") or [{}])
    base = 12 if focus in {"convection", "urban", "snowfall"} or finest_dx <= 3000 else 6
    interval = int(request.get("forecast_interval_hours") or 1)
    return next((value for value in (6, 12, 18, 24) if value >= base and value % interval == 0), 24)


class TaskNotFoundError(KeyError):
    pass


class TaskConflictError(RuntimeError):
    pass


class TaskCancelledError(RuntimeError):
    pass


class WrfTaskManager:
    def __init__(self, settings: Settings, store: TaskStore, gfs: GfsManager, hpc: HpcClient):
        self.settings = settings
        self.store = store
        self.gfs = gfs
        self.hpc = hpc
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._state_lock = threading.RLock()
        self._cancelled: set[str] = set()
        self._cancel_threads: dict[str, threading.Thread] = {}
        self._active_task_ids: set[str] = set()
        self._cycle_locks: dict[str, threading.Lock] = {}
        self._health: dict[str, Any] = {"status": "checking", "message": "等待检查超算连接"}

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

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

    def refresh_hpc_health(self) -> dict[str, Any]:
        self._check_health()
        return self.hpc_health

    def authenticate_hpc(self, password: str) -> dict[str, Any]:
        self._health = self.hpc.authenticate_password(password)
        return self.hpc_health

    def _check_health(self) -> None:
        self._health = self.hpc.health()

    def start(self) -> None:
        if any(worker.is_alive() for worker in self._workers):
            return
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
            self._queue.put(task["id"])
        self._workers = [
            threading.Thread(
                target=self._work,
                daemon=True,
                name=f"wrf-task-worker-{index + 1}",
            )
            for index in range(self.settings.max_concurrent_tasks)
        ]
        for worker in self._workers:
            worker.start()
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
        workers = list(self._workers)
        for _ in workers:
            self._queue.put(None)
        deadline = time.monotonic() + 5
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        with self._state_lock:
            cancel_threads = list(self._cancel_threads.values())
        for thread in cancel_threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if not any(worker.is_alive() for worker in workers) and not any(
            thread.is_alive() for thread in cancel_threads
        ):
            self.hpc.close_session()

    @staticmethod
    def new_task_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"wrf_gfs_{stamp}_{uuid.uuid4().hex[:8]}"

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = self.new_task_id()
        task_dir = self.settings.run_dir / task_id
        (task_dir / "raw").mkdir(parents=True, exist_ok=False)
        (task_dir / "task.request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        task = self.store.create(task_id, request)
        self._queue.put(task_id)
        self._log(task_id, f"任务已进入并行队列（最多 {self.settings.max_concurrent_tasks} 个任务）")
        return task

    def get(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list(limit)

    def active_gfs_cycles(self) -> set[str]:
        return {
            str((task.get("runtime") or {}).get("gfs_cycle"))
            for task in self.store.active()
            if (task.get("runtime") or {}).get("gfs_cycle")
        }

    def retry(self, task_id: str) -> dict[str, Any]:
        return self.submit(self.get(task_id)["request"])

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
        self._queue.put(task_id)
        self._log(task_id, "已加入结果恢复队列，仅重试下载和渲染，不重新运行 WPS/WRF")
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
            raise TaskConflictError("任务没有已确认的坏帧，不能切换为部分渲染")
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
        self._queue.put(task_id)
        self._log(task_id, "用户确认忽略坏帧，已加入部分结果渲染队列")
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

    def logs(self, task_id: str, after: int = 0, limit: int = 65536) -> dict[str, Any]:
        self.get(task_id)
        path = self._log_path(task_id)
        if not path.exists():
            return {"offset": 0, "text": ""}
        size = path.stat().st_size
        start = min(max(0, int(after)), size)
        with path.open("rb") as source:
            source.seek(start)
            data = source.read(max(1, min(int(limit), 262144)))
            offset = source.tell()
        return {"offset": offset, "text": data.decode("utf-8", errors="replace")}

    def _merge_runtime(self, task_id: str, **values: Any) -> dict[str, Any]:
        task = self.get(task_id)
        runtime = dict(task.get("runtime") or {})
        runtime.update(values)
        return self.store.update(task_id, runtime=runtime)

    def _is_cancelled(self, task_id: str) -> bool:
        with self._state_lock:
            return task_id in self._cancelled

    def _raise_if_cancelled(self, task_id: str) -> None:
        if self._is_cancelled(task_id):
            raise TaskCancelledError("任务已取消")

    def _cycle_lock(self, cycle: str) -> threading.Lock:
        with self._state_lock:
            return self._cycle_locks.setdefault(cycle, threading.Lock())

    @contextmanager
    def _prepare_cycle(self, task_id: str, cycle: str):
        lock = self._cycle_lock(cycle)
        if lock.locked():
            self.store.update(task_id, stage="waiting_for_gfs_cache", progress=4)
            self._log(task_id, f"等待其他任务完成 GFS {cycle} 缓存准备")
        while not lock.acquire(timeout=0.5):
            if self._is_cancelled(task_id):
                raise TaskCancelledError("等待 GFS 缓存期间任务已取消")
            if self._stop.is_set():
                raise RuntimeError("等待 GFS 缓存期间服务正在停止")
        try:
            self._raise_if_cancelled(task_id)
            if self._stop.is_set():
                raise RuntimeError("GFS 缓存准备开始前服务正在停止")
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

    def _complete_worker_cancellation(self, task_id: str) -> None:
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

    def _work(self) -> None:
        while not self._stop.is_set():
            task_id = self._queue.get()
            if task_id is None:
                self._queue.task_done()
                break
            try:
                task = self.get(task_id)
                if task["status"] in FINAL_STATES or self._is_cancelled(task_id):
                    continue
                with self._state_lock:
                    self._active_task_ids.add(task_id)
                self._execute(task_id)
            except TaskCancelledError:
                self._complete_worker_cancellation(task_id)
            except Exception as exc:
                if self._is_cancelled(task_id):
                    self._complete_worker_cancellation(task_id)
                else:
                    self._log(task_id, f"任务失败：{exc}")
                    self.store.update(task_id, status="failed", stage="failed", error=str(exc))
            finally:
                with self._state_lock:
                    self._active_task_ids.discard(task_id)
                self._queue.task_done()

    def _reconcile(self, task_id: str) -> str:
        self._log(task_id, "服务重启后开始与超算任务对账")
        while not self._stop.is_set():
            self._raise_if_cancelled(task_id)
            try:
                status = self.hpc.status(task_id)
            except Exception as exc:
                self._log(task_id, f"超算暂不可达，30 秒后继续对账：{exc}")
                self.store.update(task_id, status="reconciling", stage="reconciling", error=str(exc))
                self._stop.wait(30)
                continue
            if status["status"] == "running":
                self.store.update(task_id, status="running", stage="running", error=None)
                return "monitor"
            if status["status"] == "succeeded":
                return "finalize"
            if status["status"] == "failed":
                raise RuntimeError(f"远端任务失败，退出码 {status.get('exit_code', '?')}")
            self._log(task_id, "远端没有该任务，重新执行幂等的数据准备流程")
            return "restart"
        return "stop"

    def _monitor(self, task_id: str) -> bool:
        last_log = ""
        while not self._stop.is_set():
            self._raise_if_cancelled(task_id)
            status = self.hpc.status(task_id)
            remote_log = status.get("log", "")
            if remote_log and remote_log != last_log:
                self._log(task_id, "--- 远端日志快照 ---\n" + remote_log)
                last_log = remote_log
            if status["status"] == "succeeded":
                return True
            if status["status"] == "failed":
                raise RuntimeError(f"远端 WRF 退出码 {status.get('exit_code', '?')}")
            if status["status"] not in {"running"}:
                raise RuntimeError(f"远端任务状态异常：{status['status']}")
            self._stop.wait(self.settings.hpc_poll_seconds)
        return False

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
            raise RuntimeError(f"超算 wrfout 完整性校验失败：{names}；可手动选择忽略坏帧并部分渲染")
        valid_names = {str(item["name"]) for item in validation.get("valid") or [] if item.get("name")}
        run_dir = self.settings.run_dir / task_id
        raw_dir = run_dir / "raw"
        self.store.update(task_id, status="rendering", stage="downloading_outputs", progress=88)
        self._log(task_id, "下载超算 wrfout 结果")
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
        self.store.update(task_id, status=final_status, stage="done", progress=100, result=result, error=None)
        self._log(task_id, "任务部分完成，坏帧已排除并记录质量告警" if partial else "任务完成，WebP 与 scene.meta.json 已生成")

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
        start = datetime.fromisoformat(request["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(request["end_time"].replace("Z", "+00:00"))
        spinup_hours = resolve_spinup_hours(request)
        model_start = start - timedelta(hours=spinup_hours)
        self.store.update(task_id, status="prefetching", stage="selecting_cycle", progress=2, error=None)
        self._log(task_id, f"产品起报前增加 {spinup_hours} 小时 spin-up；按固定 00Z 策略选择 GFS cycle")
        self._raise_if_cancelled(task_id)
        cycle, _ = self.gfs.select_cycle(model_start, end)
        self._raise_if_cancelled(task_id)
        hours = self.gfs.required_hours(model_start, end, int(request["forecast_interval_hours"]), cycle)
        self._merge_runtime(
            task_id,
            gfs_cycle=cycle,
            forecast_hours=hours,
            spinup_hours=spinup_hours,
            model_start_time=model_start.isoformat().replace("+00:00", "Z"),
        )

        with self._prepare_cycle(task_id, cycle):
            self.store.update(task_id, stage="checking_hpc_gfs", progress=4)
            self._log(task_id, f"仅检查超算 GFS 数据池：{cycle}，共 {len(hours)} 个时次")
            last_ready = -1

            def remote_gfs_progress(snapshot: dict[str, Any]) -> None:
                nonlocal last_ready
                ready = len(snapshot.get("valid_hours") or [])
                if ready != last_ready:
                    last_ready = ready
                    self._log(task_id, f"等待超算共享下载：{ready}/{len(hours)} 个任务时次已就绪")
                self._merge_runtime(
                    task_id,
                    gfs_remote_reused=ready,
                    gfs_remote_missing=len(snapshot.get("missing_hours") or []),
                    gfs_total=len(hours),
                    gfs_download_owner="hpc_shared_pool",
                )
                self.store.update(
                    task_id,
                    progress=5 + int(55 * ready / max(1, len(hours))),
                    stage="waiting_for_hpc_gfs",
                )

            ensure_remote = getattr(self.hpc, "ensure_remote_gfs", None)
            if ensure_remote:
                remote_cache = ensure_remote(
                    cycle,
                    hours,
                    progress=remote_gfs_progress,
                    cancelled=lambda: self._is_cancelled(task_id) or self._stop.is_set(),
                )
            else:
                remote_cache = self.hpc.inspect_gfs_files(cycle, hours)
                if remote_cache.get("missing_hours"):
                    raise RuntimeError("超算 GFS 数据池不完整，且连接客户端不支持远端共享下载")
            verified_entries = list(remote_cache.get("entries") or [])
            self._merge_runtime(
                task_id,
                gfs_remote_reused=len(remote_cache.get("valid_hours") or []),
                gfs_remote_missing=0,
                gfs_total=len(hours),
                gfs_download_owner="hpc_shared_pool",
            )
            self._log(task_id, "超算完整 GFS 文件校验通过，任务将直接复用共享数据池")
            self.store.update(task_id, progress=65, stage="remote_gfs_ready")
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
            self._log(task_id, f"超算文件传输：{marker[0]} - {marker[1]}")

        self._raise_if_cancelled(task_id)
        run_dir = self.settings.run_dir / task_id
        config_path = run_dir / "task.config.json"
        environment_path = run_dir / "task.env"
        expected_gfs_path = run_dir / "gfs.expected.tsv"
        write_task_bundle(
            task_id,
            request,
            cycle,
            hours,
            verified_entries,
            config_path,
            environment_path,
            expected_gfs_path,
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
                expected_gfs_path,
                Path(__file__).resolve().parent / "scripts",
                progress=prepare_progress,
            )
        finally:
            sync_transfer_status()

        self._raise_if_cancelled(task_id)
        self.store.update(task_id, status="running", stage="running", progress=68)
        launch = self.hpc.launch(task_id)
        self._merge_runtime(task_id, **launch)
        self._log(task_id, f"超算任务已启动，PID {launch['remote_pid']}")
        if self._is_cancelled(task_id):
            self.store.update(task_id, status="cancel_pending", stage="cancel_pending")
            self._start_remote_cancel(task_id)
            raise TaskCancelledError("任务在远端进程启动时收到取消请求")
        if not self._monitor(task_id):
            return
        self._merge_runtime(task_id, remote_wrf_succeeded=True)
        self._finalize(task_id, cycle)
