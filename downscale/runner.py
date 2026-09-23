from __future__ import annotations

import json
import tarfile
import threading
import tempfile
import time
from pathlib import Path
from typing import Any

from config import Settings
from hpc_transport import HpcClient

from .service import DownscaleService
from .runner_contract import stage_runner_contract
from .upstream import build_source_artifact
from .schemas import DownscaleValidationRequest


class DownscaleRunner:
    """Single-flight executor for the high-resolution route.

    The runner owns the complete transition from a queued API task to the
    remote WRF-LES forcing and, for production full-chain contracts, CFD product
    stages, then publishes only a validated product bundle.
    """

    def __init__(self, settings: Settings, service: DownscaleService, hpc: HpcClient):
        self.settings = settings
        self.service = service
        self.hpc = hpc
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: str | None = None
        self._lock = threading.RLock()

    @property
    def active_task_id(self) -> str | None:
        with self._lock:
            return self._active

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wrf-downscale-runner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        poll_seconds = max(2, int(getattr(self.settings, "downscale_runner_poll_seconds", 10)))
        while not self._stop.is_set():
            task = self._next_task()
            if task:
                with self._lock:
                    self._active = str(task["id"])
                try:
                    try:
                        self._execute(task)
                    except Exception as exc:
                        self._fail(str(task["id"]), f"运行器未处理异常：{exc}", stage="runner_internal_error")
                finally:
                    with self._lock:
                        self._active = None
            self._stop.wait(poll_seconds)

    def _next_task(self) -> dict[str, Any] | None:
        for task in self.service.store.list(200, None):
            status = task.get("status")
            runtime = task.get("runtime") or {}
            if status == "failed" and task.get("stage") == "remote_status_failed" and runtime.get("remote_launch_attempted"):
                self.service.store.append_log(task["id"], "检测到旧版轮询误判；任务进入远端状态自动对账。")
                task = self.service.store.update(
                    task["id"], status="reconciling", stage="remote_status_retrying",
                    progress=int(task.get("progress") or 0), error=None,
                ) or task
                status = "reconciling"
            if status == "running" and not runtime.get("remote_launch_attempted"):
                self.service.store.update(task["id"], status="queued", stage="queued", progress=0)
                status = "queued"
            if status == "waiting_upstream" and self.service.normal_task_lookup:
                source = runtime.get("source_wrf") or {}
                normal = self.service.normal_task_lookup(str(source.get("task_id") or ""), task["owner_sub"], int(source.get("owner_role") or 1))
                if normal and normal.get("status") not in {"succeeded", "failed", "cancelled", "waiting_restart"}:
                    continue
            if status in {"queued", "waiting_upstream"} or (status in {"running", "reconciling"} and runtime.get("remote_launch_attempted")):
                return task
        return None

    def _fail(self, task_id: str, message: str, *, stage: str = "failed") -> None:
        message = str(message).replace("\n", " ")[:2000]
        self.service.store.append_log(task_id, f"高分运行器失败：{message}")
        current = self.service.store.get(task_id) or {}
        request = current.get("request") or {}
        composite = request.get("execution_scope") == "wrf_cfd" and request.get("upstream_mode") == "full_chain"
        cfd_partial = composite and (stage.startswith("cfd_") or stage in {"cfd_failed", "source_forcing_handoff", "surface_prepare", "forcing_ready"})
        self.service.store.update(
            task_id,
            status="partial_success" if cfd_partial else "failed",
            stage=stage,
            progress=0,
            error=message,
            quality={"status": "pending", "gates": ([{"id": "mesoscale_forcing", "status": "pass"}] if cfd_partial else [])},
            publication={"status": "blocked"},
        )

    def _execute(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        runtime = dict(task.get("runtime") or {})
        source = runtime.get("source_wrf") or {}
        artifact = source.get("artifact") or {}
        artifact_needs_refresh = not artifact or not artifact.get("source_cycle") or not artifact.get("source_forecast_hours")
        if artifact_needs_refresh:
            source_id = str(source.get("task_id") or (task.get("request") or {}).get("source_wrf_task_id") or "")
            if not source_id or self.service.normal_task_lookup is None:
                self._fail(task_id, "普通 WRF 上游没有可交接的原生 wrfout/wrfrst 清单", stage="upstream_artifact_missing")
                return
            source_owner_role = int(source.get("owner_role") or 1)
            normal = self.service.normal_task_lookup(source_id, str(task.get("owner_sub") or ""), source_owner_role)
            if not normal or normal.get("status") in {"failed", "cancelled", "waiting_restart"}:
                self._fail(task_id, f"上游 WRF {source_id} 未完成：{(normal or {}).get('error') or (normal or {}).get('status') or '不存在或无权访问'}", stage="upstream_failed")
                return
            if normal.get("status") != "succeeded":
                self.service.store.update(task_id, status="waiting_upstream", stage="waiting_upstream", progress=2, error=None)
                return
            if self.service.source_artifact_lookup is None:
                self._fail(task_id, "无法读取普通 WRF 上游原生输出清单", stage="upstream_artifact_missing")
                return
            inspected = self.service.source_artifact_lookup(source_id, str(task.get("owner_sub") or ""), source_owner_role)
            if not inspected or inspected.get("error"):
                self._fail(task_id, str((inspected or {}).get("error") or "普通 WRF 上游原生输出清单不可用"), stage="upstream_artifact_missing")
                return
            try:
                artifact = build_source_artifact(normal, inspected)
                request = DownscaleValidationRequest.model_validate(task["request"])
                self.service._validate_source_compatibility(request, artifact)
            except Exception as exc:
                self._fail(task_id, str(exc), stage="upstream_artifact_invalid")
                return
            source = {**source, "task_id": source_id, "status": "succeeded", "owner_role": source_owner_role, "artifact": artifact}
            runtime["source_wrf"] = source
            if artifact_needs_refresh:
                self.service.store.append_log(task_id, f"已从普通 WRF 上游刷新实际驱动周期 {artifact.get('source_cycle')} 和时效清单。")
            self.service.store.update(task_id, runtime=runtime, status="queued", stage="preparing_remote", progress=4)
            contract_path = self.settings.run_dir / "downscale_jobs" / task_id / "downscale.job.json"
            contract = stage_runner_contract(self.settings.run_dir / "downscale_jobs", task_id, task["request"], runtime)
            runtime["runner_contract"] = contract
            self.service.store.update(task_id, runtime=runtime)
        contract_path = self.settings.run_dir / "downscale_jobs" / task_id / "downscale.job.json"
        if not contract_path.is_file():
            self._fail(task_id, f"受管运行合同不存在：{contract_path}", stage="runner_contract_missing")
            return

        reattach = task.get("status") in {"running", "reconciling"} and bool(runtime.get("remote_launch_attempted"))
        if not reattach:
            self.service.store.update(task_id, status="running", stage="preparing_remote", progress=5, error=None)
            self.service.store.append_log(task_id, "开始上传高分运行合同、数据盘保护脚本和上游交接任务")
            try:
                surface = runtime.get("surface") or {}
                if surface.get("mode") == "reviewed_terrain":
                    with tempfile.TemporaryDirectory(prefix=f"wrf-surface-{task_id}-") as temporary:
                        assets = self.service.download_terrain_assets(surface, Path(temporary))
                        self.hpc.prepare_downscale_runtime(
                            task_id, contract_path, Path(__file__).resolve().parent.parent / "scripts", assets,
                        )
                else:
                    self.hpc.prepare_downscale_runtime(
                        task_id, contract_path, Path(__file__).resolve().parent.parent / "scripts",
                    )
                runtime["remote_prepare_complete"] = True
                self.service.store.update(task_id, runtime=runtime, progress=12, stage="checking_upstream_restart")
                launch = self.hpc.launch_downscale(task_id)
                runtime.update({"remote_launch_attempted": True, **launch})
                self.service.store.update(task_id, runtime=runtime, progress=15, stage="source_handoff_running")
                self.service.store.append_log(task_id, f"远端高分交接任务已启动，PID {launch['remote_pid']}")
            except Exception as exc:
                self._fail(task_id, str(exc), stage="remote_prepare_failed")
                return
        else:
            self.service.store.append_log(task_id, "服务重启后重新接管远端高分交接任务")

        remote_status_errors = 0
        remote_status_error_started = 0.0
        while not self._stop.is_set():
            try:
                status = self.hpc.downscale_status(task_id)
            except Exception as exc:
                remote_status_errors += 1
                if not remote_status_error_started:
                    remote_status_error_started = time.monotonic()
                    self.service.store.append_log(task_id, f"远端状态暂不可读，保留远端进程并开始自动对账：{exc}")
                runtime.update({
                    "remote_status_retry_count": remote_status_errors,
                    "remote_status_last_error": str(exc).replace("\n", " ")[:500],
                })
                current = self.service.store.get(task_id) or {}
                self.service.store.update(
                    task_id, status="reconciling", stage="remote_status_retrying",
                    progress=int(current.get("progress") or 0), runtime=runtime,
                    error="与 tx-lab 的连接暂时中断，远端计算保留，系统正在自动重连",
                )
                self._stop.wait(max(5, int(getattr(self.settings, "hpc_reconcile_interval_seconds", 30))))
                continue
            if remote_status_errors:
                elapsed = max(0, int(time.monotonic() - remote_status_error_started))
                self.service.store.append_log(task_id, f"tx-lab 连接已恢复，经过 {remote_status_errors} 次重试和 {elapsed} 秒后重新接管远端任务。")
                runtime.pop("remote_status_retry_count", None)
                runtime.pop("remote_status_last_error", None)
                self.service.store.update(task_id, status="running", runtime=runtime, error=None)
                remote_status_errors = 0
                remote_status_error_started = 0.0
            state = str(status.get("status") or "unknown")
            if state == "running":
                self._sync_remote_stage(task_id)
                self._stop.wait(max(2, int(getattr(self.settings, "downscale_runner_poll_seconds", 10))))
                continue
            if state != "succeeded":
                current = self.service.store.get(task_id) or {}
                current_stage = str(current.get("stage") or "")
                if current_stage in {"wrf_les_running", "source_handoff", "wps_prepare", "real_prepare", "outer_wrf", "ndown_wrf_les", "ndown_d04_d05", "wrf_d05_333m", "ndown_d05_d06", "wrf_d06_111m", "ndown_d06_d07", "wrf_d07_37m", "ndown_d07_d08", "wrf_d08_10m"}:
                    failure_stage = "wrf_les_failed"
                elif current_stage.startswith("cfd_") or current_stage in {"source_forcing_handoff", "surface_prepare", "forcing_ready", "preparing_forcing", "meshing", "solving", "generating_products", "downloading_products"}:
                    failure_stage = "cfd_failed"
                else:
                    failure_stage = "source_handoff_failed"
                self._fail(task_id, f"远端高分链失败：{status.get('log') or state}", stage=failure_stage)
                return
            break

        if self._stop.is_set():
            return
        try:
            stage = self._read_stage(task_id)
        except Exception as exc:
            self._fail(task_id, f"远端交接完成但阶段清单不可读：{exc}", stage="source_handoff_manifest_missing")
            return
        if stage.get("status") in {"source_handoff", "source_handoff_ready"} and stage.get("ready_for_ndown"):
            runtime["source_handoff"] = stage
            self.service.store.append_log(task_id, "普通 WRF d04 原生输出交接完成；任务进入四级 ndown/约 10 m WRF-LES forcing 阶段")
            self.service.store.update(
                task_id,
                status="partial_success",
                stage="waiting_ndown_wrf_les",
                progress=25,
                runtime=runtime,
                result={
                    "stage": "source_handoff",
                    "next_stage": "ndown_d04_d05",
                    "source_handoff": stage,
                    "products_ready": False,
                },
                quality={"status": "pending", "gates": [{"id": "normal_wrf_source_handoff", "status": "pass"}]},
                publication={"status": "blocked"},
                error="已完成普通 WRF d04 原生输出交接，约 10 m WRF-LES forcing 与 CFD 尚未完成",
            )
            return
        if stage.get("status") == "cfd_product_ready":
            self._publish_remote_bundle(task_id, runtime, stage)
            return
        if stage.get("status") == "forcing_ready":
            request = (self.service.store.get(task_id) or {}).get("request") or {}
            if request.get("execution_scope") == "wrf_cfd" and request.get("upstream_mode") == "full_chain":
                self._fail(task_id, "完整高分任务已生成 d08 forcing，但 CFD 阶段未启动，拒绝把中间 forcing 标记为最终成功", stage="cfd_not_started")
                return
            runtime["forcing"] = stage
            try:
                forcing_manifest = self._read_forcing_manifest(task_id)
            except Exception as exc:
                self._fail(task_id, f"forcing 已完成但 manifest 无法读取：{exc}", stage="forcing_manifest_missing")
                return
            runtime["forcing_manifest"] = forcing_manifest
            self.service.store.update(
                task_id, status="succeeded", stage="done", progress=100, runtime=runtime,
                result={"stage": "forcing_ready", "products_ready": True, "mesoscale_forcing_manifest": "mesoscale_forcing_manifest.json", "forcing_manifest": forcing_manifest},
                quality={"status": "complete", "gates": [{"id": "mesoscale_forcing", "status": "pass"}]},
                publication={"status": "internal_preview", "mode": "backend_wrf_static"}, error=None,
            )
            return
        if stage.get("status") != "wrf_les_ready":
            self._fail(task_id, "远端高分链未形成可发布的 CFD 产品阶段清单", stage="wrf_les_gate_failed")
            return
        runtime["wrf_les"] = stage
        self.service.store.append_log(task_id, "历史 34 m WRF 分级链完成；任务进入 CFD 1 m 阶段")
        self.service.store.update(task_id, status="running", stage="cfd_1m_prepare", progress=66, runtime=runtime, error=None)

    def _publish_remote_bundle(self, task_id: str, runtime: dict[str, Any], stage: dict[str, Any]) -> None:
        self.service.store.update(task_id, status="running", stage="downloading_products", progress=97, runtime=runtime, error=None)
        self.service.store.append_log(task_id, "远端 CFD 产品已完成，开始下载并校验产品包")
        with tempfile.TemporaryDirectory(prefix=f"wrf-downscale-{task_id}-") as temporary:
            temporary_root = Path(temporary)
            archive = temporary_root / "downscale.product.bundle.tar.gz"
            self.hpc.download_downscale_bundle(task_id, archive, timeout=3600)
            bundle = temporary_root / "bundle"
            bundle.mkdir()
            self._extract_bundle(archive, bundle)
            completed = self.service.publish_bundle(task_id, bundle)
        runtime["cfd"] = stage
        self.service.store.update(task_id, runtime=runtime, result=completed.get("result"), quality=completed.get("quality"), publication=completed.get("publication"), status="succeeded", stage="done", progress=100, error=None)

    @staticmethod
    def _extract_bundle(archive: Path, target: Path) -> None:
        """Extract only regular, relative product files from the remote archive."""
        allowed_suffixes = {".webp", ".json", ".geojson", ".bin", ".png", ".nc", ".tif", ".tiff", ".vtk", ".vtu", ".vtp", ".pdf", ".csv"}
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                relative = Path(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("远端产品包包含非法文件条目")
                if member.isdir():
                    (target / relative).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError("远端产品包包含非普通文件条目")
                if relative.name != "downscale.product.json" and relative.suffix.lower() not in allowed_suffixes:
                    raise ValueError(f"远端产品包包含不允许的文件类型：{member.name}")
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError(f"远端产品包文件不可读取：{member.name}")
                with destination.open("wb") as output:
                    output.write(source.read())

    def _sync_remote_stage(self, task_id: str) -> None:
        """Reflect the long-running WPS/WRF chain without trusting it as success."""
        try:
            stage = self._read_stage(task_id)
        except Exception:
            return
        if stage.get("status") not in {"wrf_les_running", "source_handoff", "ndown_d04_d05", "wrf_d05_333m", "ndown_d05_d06", "wrf_d06_111m", "ndown_d06_d07", "wrf_d07_37m", "ndown_d07_d08", "wrf_d08_10m", "forcing_ready", "source_forcing_handoff", "cfd_1m_interface", "cfd_1m_mesh", "cfd_1m_solver", "cfd_1m_export", "preparing_forcing", "meshing", "solving", "generating_products"}:
            return
        next_stage = str(stage.get("next_stage") or "")
        progress = {
            "source_handoff": 20,
            "ndown_d04_d05": 28,
            "wrf_d05_333m": 36,
            "ndown_d05_d06": 44,
            "wrf_d06_111m": 52,
            "ndown_d06_d07": 60,
            "wrf_d07_37m": 68,
            "ndown_d07_d08": 76,
            "wrf_d08_10m": 90,
            "forcing_ready": 60,
            "wps_prepare": 28,
            "real_prepare": 36,
            "outer_wrf": 50,
            "ndown_wrf_les": 60,
            "source_forcing_handoff": 62,
            "cfd_1m_interface": 68,
            "cfd_1m_mesh": 73,
            "cfd_1m_solver": 82,
            "cfd_1m_export": 90,
        }.get(next_stage, 20)
        self.service.store.update(
            task_id,
            status="running",
            stage=str(stage.get("status") or next_stage or "wrf_les_running"),
            progress=progress,
            error=None,
        )

    def _read_stage(self, task_id: str) -> dict[str, Any]:
        remote = f"{self.hpc.downscale_output_dir(task_id)}/high-resolution-stage.json"
        raw = self.hpc.run(f"cat {self.hpc._quote_remote(remote)}", timeout=60)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("阶段清单不是 JSON 对象")
        return value

    def _read_forcing_manifest(self, task_id: str) -> dict[str, Any]:
        remote = f"{self.hpc.downscale_output_dir(task_id)}/mesoscale_forcing_manifest.json"
        raw = self.hpc.run(f"cat {self.hpc._quote_remote(remote)}", timeout=120)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("forcing manifest 不是 JSON 对象")
        return value
