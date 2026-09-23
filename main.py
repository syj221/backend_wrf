from __future__ import annotations

from contextlib import asynccontextmanager
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from auth import install_auth
from database import TaskStore
from gfs import GfsManager
from hpc import HpcClient, HpcError
from recommendations import RecommendationManager
from schemas import (
    FIXED_SPINUP_HOURS,
    HpcAuthRequest,
    RemoteGfsCleanupRequest,
    RemoteGfsTriggerRequest,
    TaskDeleteRequest,
    WrfRecommendationRequest,
    WrfTaskCreate,
    WrfTaskRestartRequest,
    normalize_task_request,
)
from task_manager import TaskConflictError, TaskNotFoundError, WrfTaskManager
from downscale import DownscaleService, build_router as build_downscale_router
from downscale.runner import DownscaleRunner
from wrf_evidence_api import router as wrf_evidence_router


settings.ensure_directories()
store = TaskStore(settings.database_path)
gfs_manager = GfsManager(settings)
hpc_client = HpcClient(settings)
task_manager = WrfTaskManager(settings, store, gfs_manager, hpc_client)
recommendation_manager = RecommendationManager(settings, hpc_client)


def lookup_normal_wrf_task(task_id: str, owner_sub: str, owner_role: int) -> dict[str, Any] | None:
    try:
        task = task_manager.get(task_id)
    except TaskNotFoundError:
        return None
    if owner_role < 3 and task.get("owner_sub") != owner_sub:
        return None
    return task


def lookup_normal_wrf_artifact(task_id: str, owner_sub: str, owner_role: int) -> dict[str, Any] | None:
    """Return a remote native-file inventory after the normal task ACL gate."""
    if lookup_normal_wrf_task(task_id, owner_sub, owner_role) is None:
        return None
    try:
        return hpc_client.inspect_wrf_source(task_id)
    except (HpcError, ValueError) as exc:
        return {"error": str(exc), "files": []}


def lookup_downscale_forcing_manifest(task: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh a completed forcing manifest when it predates local caching."""
    runtime = task.get("runtime") or {}
    output_dir = str(runtime.get("remote_output_dir") or ((runtime.get("forcing") or {}).get("remote_output_dir") or ""))
    if not output_dir:
        return None
    try:
        return hpc_client.inspect_downscale_forcing_manifest(output_dir)
    except (HpcError, ValueError, json.JSONDecodeError):
        return None


def submit_auto_normal_wrf_task(request: dict[str, Any], owner_sub: str, idempotency_key: str | None) -> dict[str, Any]:
    """Create the 1 km upstream task for a one-click high-resolution run."""
    if idempotency_key:
        return task_manager.submit_idempotent(request, owner_sub, idempotency_key)
    return task_manager.submit(request, owner_sub)


downscale_service = DownscaleService(
    settings,
    normal_task_lookup=lookup_normal_wrf_task,
    source_artifact_lookup=lookup_normal_wrf_artifact,
    forcing_manifest_lookup=lookup_downscale_forcing_manifest,
    normal_task_submit=submit_auto_normal_wrf_task,
    normal_task_cancel=task_manager.cancel,
)
downscale_runner = DownscaleRunner(settings, downscale_service, hpc_client)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task_manager.start()
    if settings.downscale_runner_enabled:
        downscale_runner.start()
    try:
        yield
    finally:
        downscale_runner.stop()
        task_manager.stop()
        recommendation_manager.stop()
        downscale_service.close()
        store.close()


app = FastAPI(
    title="Weather WRF Backend",
    description="tx-lab 共享 GFS 数据池调度、WPS/WRF 执行与 WebP 结果发布微服务。",
    version="1.0.0",
    lifespan=lifespan,
)

install_auth(
    app,
    [
        ("/api/wrf/tasks", {"GET": 1, "POST": 2, "DELETE": 2}),
        # 专业高分配置可由所有已登录 WRF 用户提交；资产与认证审核
        # 仍在 downscale router 内强制要求管理员角色。
        ("/api/wrf/downscale", {"GET": 1, "POST": 1, "DELETE": 1}),
        ("/api/wrf/workbench/tasks", {"GET": 1}),
        ("/api/wrf/data-status", {"GET": 1, "*": 2}),
        ("/api/wrf/gfs", {"GET": 1, "*": 2}),
        ("/api/wrf/forcing", {"GET": 1, "*": 2}),
        ("/api/wrf/recommendations", {"GET": 1, "*": 2}),
        ("/api/wrf/evidence", {"GET": 2, "POST": 2}),
        ("/api/wrf/options", {"GET": 1, "*": 2}),
        ("/api/wrf/hpc", 2),
        ("/api/wrf/display", 1),
        ("/data/WRF/", 1),
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/data/WRF", StaticFiles(directory=settings.output_dir), name="wrf-data")


def ok(data: Any = None, message: str = "success") -> dict[str, Any]:
    return {"code": 0, "data": data, "message": message}


def get_task(task_id: str) -> dict[str, Any]:
    try:
        return task_manager.get(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None


def _viewer(request: Request) -> tuple[str, int]:
    user = getattr(request.state, "user", None) or {}
    return str(user.get("sub") or ""), int(user.get("role") or 0)


def _normal_visible_tasks(request: Request, limit: int) -> list[dict[str, Any]]:
    owner_sub, role = _viewer(request)
    return task_manager.list(limit, None if role >= 3 else owner_sub)


app.include_router(build_downscale_router(downscale_service, _viewer, _normal_visible_tasks))
app.include_router(wrf_evidence_router)


def get_visible_task(task_id: str, request: Request) -> dict[str, Any]:
    task = get_task(task_id)
    owner_sub, role = _viewer(request)
    if role >= 3 or (owner_sub and task.get("owner_sub") == owner_sub):
        return task
    # 旧任务没有可验证的归属，只向系统管理员开放，避免普通用户越权读取。
    raise HTTPException(status_code=404, detail="WRF 任务不存在")


@app.get("/")
def root() -> dict[str, Any]:
    return ok({"service": "backend_wrf", "docs": "/docs"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    hpc_health = task_manager.hpc_health
    if hpc_health.get("status") != "ready":
        hpc_health = task_manager.refresh_hpc_health()
    hpc_health["transfer"] = hpc_client.transfer_status
    hpc_health["ssh_port"] = settings.hpc_port
    return ok(
        {
            "status": "online",
            "mode": "tx_lab_remote_task_service",
            "queue_size": task_manager.queue_size,
            "active_task_id": task_manager.active_task_id,
            "active_task_ids": task_manager.active_task_ids,
            "active_task_count": task_manager.active_task_count,
            "scheduling_mode": "dynamic",
            "hpc": hpc_health,
            "gfs": {
                "mode": "tx_lab_remote_pool",
                "scope": "global",
                "bounds": hpc_client.gfs_bounds,
                "remote_root": settings.hpc_gfs_dir,
                "storage_mount": settings.hpc_gfs_mount,
                "min_free_gb": settings.hpc_gfs_min_free_gb,
                "prefetch": task_manager.gfs_prefetch_status,
            },
            "forcing": {"mode": "tx_lab_remote_pool", "data_sources": ["gfs", "ecmwf"]},
            "downscale": {
                "runner_enabled": settings.downscale_runner_enabled,
                "active_task_id": downscale_runner.active_task_id,
                "stage_boundary": "normal_wrf_source_handoff",
            },
        }
    )


@app.get("/api/wrf/options")
def options() -> dict[str, Any]:
    return ok(
        {
            "capabilities": {
                "data_sources": ["GFS", "ECMWF"],
                "execution_modes": ["TX_LAB"],
                "max_domains": 4,
                "scheduling_mode": "dynamic",
                "runtime_profiles": ["cpu"],
                "default_runtime_profile": "cpu",
                "fixed_spinup_hours": FIXED_SPINUP_HOURS,
                "gfs_cycle_hours": [0],
                "gfs_cycle_policy": "latest_covering_00z",
                "gfs_product": "gfs-0p25-global",
                "gfs_scope": "global",
                "gfs_bounds": hpc_client.gfs_bounds,
                "gfs_download_mode": "hpc_remote_nomads_grib_filter",
                "gfs_storage_path": settings.hpc_gfs_dir,
                "ecmwf_cycle_hours": [0],
                "ecmwf_cycle_policy": "latest_covering_00z",
                "ecmwf_product": "ecmwf-ifs-0p25",
                "ecmwf_download_mode": "tx_lab_remote_full_file",
                "ecmwf_storage_path": settings.hpc_ec_dir,
                "gfs_storage_mount": settings.hpc_gfs_mount,
                "gfs_min_free_gb": settings.hpc_gfs_min_free_gb,
                "gfs_request_interval_seconds": settings.hpc_gfs_request_interval_seconds,
                "gfs_prefetch_enabled": settings.hpc_gfs_prefetch_enabled,
                "gfs_prefetch_interval_seconds": settings.hpc_gfs_prefetch_interval_seconds,
                "gfs_retained_cycles": settings.hpc_gfs_retained_cycles,
                "pool_history_retention_days": settings.hpc_pool_history_retention_days,
                "gfs_min_speed_bps": settings.hpc_gfs_min_speed_bps,
                "gfs_slow_seconds": settings.hpc_gfs_slow_seconds,
                "hpc_transfer_mode": settings.hpc_transfer_mode,
                "hpc_import_legacy_gfs": settings.hpc_import_legacy_gfs,
                "downscale_runner_enabled": settings.downscale_runner_enabled,
                "downscale_runner_poll_seconds": settings.downscale_runner_poll_seconds,
            },
            "forecast_intervals": [1, 3, 6],
            "assimilation_schemes": [
                {"value": "off", "label": "关闭"},
                {"value": "fdda_weak", "label": "弱网格松弛"},
                {"value": "fdda_standard", "label": "标准网格松弛"},
                {"value": "fdda_strong", "label": "强网格松弛"},
            ],
            "physics_presets": {
                "默认通用": {"mp_physics": 8, "cu_physics": 0, "ra_lw_physics": 4, "ra_sw_physics": 4, "bl_pbl_physics": 1, "sf_sfclay_physics": 1, "sf_surface_physics": 2, "sf_urban_physics": 0, "num_soil_layers": 4, "num_land_cat": 21, "radt": 5},
                "热带对流": {"mp_physics": 6, "cu_physics": 5, "ra_lw_physics": 4, "ra_sw_physics": 4, "bl_pbl_physics": 1, "sf_sfclay_physics": 1, "sf_surface_physics": 2, "sf_urban_physics": 0, "num_soil_layers": 4, "num_land_cat": 21, "radt": 5},
                "冬季降雪": {"mp_physics": 16, "cu_physics": 0, "ra_lw_physics": 4, "ra_sw_physics": 4, "bl_pbl_physics": 2, "sf_sfclay_physics": 2, "sf_surface_physics": 2, "sf_urban_physics": 0, "num_soil_layers": 4, "num_land_cat": 21, "radt": 5},
            },
            "default_domain_count": 1,
            "default_domains": [
                {"id": "d01", "dx": 27000, "dy": 27000, "e_we": 100, "e_sn": 79, "parent_id": 0, "parent_grid_ratio": 1, "i_parent_start": 1, "j_parent_start": 1},
                {"id": "d02", "dx": 9000, "dy": 9000, "e_we": 79, "e_sn": 61, "parent_id": 1, "parent_grid_ratio": 3, "i_parent_start": 11, "j_parent_start": 11},
                {"id": "d03", "dx": 3000, "dy": 3000, "e_we": 64, "e_sn": 46, "parent_id": 2, "parent_grid_ratio": 3, "i_parent_start": 6, "j_parent_start": 6},
                {"id": "d04", "dx": 1000, "dy": 1000, "e_we": 55, "e_sn": 40, "parent_id": 3, "parent_grid_ratio": 3, "i_parent_start": 6, "j_parent_start": 6},
            ],
        }
    )


@app.get("/api/wrf/data-status")
def data_status() -> dict[str, Any]:
    try:
        pool_items: list[dict[str, Any]] = []
        status_priority = {
            "failed": 5, "error": 5, "rate_limited": 4, "downloading": 3,
            "retry_wait": 4, "waiting_publish": 2, "partial": 2, "ready": 1, "idle": 0,
        }
        overall = "idle"
        hpc_health = task_manager.hpc_health
        for source in ("gfs", "ecmwf"):
            target_cycles = gfs_manager.latest_cycles(
                count=settings.hpc_gfs_retained_cycles,
                data_source=source,
            )
            active_cycles = task_manager.active_forcing_cycles(source)
            retained_cycles = task_manager.retained_gfs_cycles() if source == "gfs" else set()
            items = hpc_client.forcing_pool_items(
                source,
                target_cycles,
                active_cycles | retained_cycles,
            )
            for item in items:
                if status_priority.get(str(item.get("status")), 0) > status_priority.get(overall, 0):
                    overall = str(item.get("status"))
                for cycle in item.get("cycles") or []:
                    key = str(cycle.get("cycle") or "")
                    cycle["task_required"] = key in active_cycles
                    cycle["retained"] = key in retained_cycles
                    cycle["prefetch_target"] = key in target_cycles
            pool_items.extend(items)
        return ok({
            "status": overall,
            "mode": "tx_lab_remote_pool",
            "scope": "global",
            "bounds": hpc_client.gfs_bounds,
            "storage_path": settings.hpc_gfs_dir,
            "storage_free_gb": hpc_health.get("gfs_free_gb"),
            "runtime_free_gb": hpc_health.get("runtime_free_gb"),
            "prefetch": task_manager.gfs_prefetch_status,
            "pool_items": pool_items,
        })
    except Exception as exc:
        return ok({"status": "unavailable", "mode": "tx_lab_remote_pool", "message": str(exc), "pool_items": []})


@app.post("/api/wrf/forcing/prepare", status_code=202)
def prepare_all_forcing() -> dict[str, Any]:
    """Check GFS and ECMWF pools and start missing forecast downloads."""
    try:
        gfs = task_manager.sync_latest_gfs()
        ec = sync_latest_remote_forcing("ecmwf").get("data") or {}
        return ok({"providers": {"gfs": gfs, "ecmwf": ec}})
    except (ValueError, HpcError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/forcing/{data_source}/sync-latest", status_code=202)
def sync_latest_remote_forcing(data_source: str) -> dict[str, Any]:
    source = gfs_manager.normalize_source(data_source)
    if source == "gfs":
        return sync_latest_remote_gfs()
    try:
        result = task_manager.sync_latest_ecmwf()
        return ok({"data_source": source, **result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/forcing/{data_source}/cleanup")
def cleanup_remote_forcing(data_source: str, request: RemoteGfsCleanupRequest) -> dict[str, Any]:
    source = gfs_manager.normalize_source(data_source)
    target_cycles = gfs_manager.latest_cycles(count=settings.hpc_gfs_retained_cycles, data_source=source)
    try:
        result = hpc_client.cleanup_forcing_cycles(
            source, request.paths, target_cycles, task_manager.active_forcing_cycles(source)
        )
        return ok(result, message=f"已清理确认的 tx-lab 旧 {source.upper()} 周期")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/forcing/{data_source}/trigger", status_code=202)
def trigger_remote_forcing(data_source: str, request: RemoteGfsTriggerRequest) -> dict[str, Any]:
    source = gfs_manager.normalize_source(data_source)
    try:
        return ok(hpc_client.trigger_forcing_download(source, request.cycle, 72))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/gfs/sync-latest", status_code=202)
def sync_latest_remote_gfs() -> dict[str, Any]:
    try:
        result = task_manager.sync_latest_gfs()
        return ok(
            result,
            message=f"tx-lab 最近 {settings.hpc_gfs_retained_cycles} 个全球 GFS 00Z 周期已同步",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/gfs/cleanup")
def cleanup_remote_gfs(request: RemoteGfsCleanupRequest) -> dict[str, Any]:
    target_cycles = gfs_manager.latest_cycles()
    try:
        result = hpc_client.cleanup_gfs_cycles(
            request.paths,
            target_cycles,
            task_manager.protected_gfs_cycles(),
        )
        return ok(result, message="已清理确认的 tx-lab 旧 GFS 周期")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/gfs/trigger", status_code=202)
def trigger_remote_gfs(request: RemoteGfsTriggerRequest) -> dict[str, Any]:
    try:
        protected_cycles = task_manager.protected_gfs_cycles()
        auto_cleanup = hpc_client.auto_cleanup_gfs_cycles([request.cycle], protected_cycles)
        result = hpc_client.trigger_gfs_download(request.cycle, 72)
        result["auto_cleanup"] = auto_cleanup
        return ok(result, message="tx-lab 共享 GFS 已自动清理并检查下载状态")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/recommendations", status_code=202)
def create_recommendation(request: WrfRecommendationRequest) -> dict[str, Any]:
    return ok(
        recommendation_manager.submit(request.model_dump(mode="json")),
        message="已提交 tx-lab geogrid 地理分析",
    )


@app.get("/api/wrf/recommendations/{job_id}")
def recommendation_detail(job_id: str) -> dict[str, Any]:
    job = recommendation_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="WRF 参数推荐任务不存在")
    return ok(job)


@app.post("/api/wrf/tasks/validate")
def validate_task(payload: WrfTaskCreate) -> dict[str, Any]:
    return ok({"request": normalize_task_request(payload.model_dump(mode="json")),
               "warnings": ["时间按 UTC 解释；固定 CPU 运行与 6 小时 spin-up",
                            "已通过配置规则校验；不代表驱动资料和 HPC 当前可用", "计算将消耗资源，需显式确认"]})


@app.post("/api/wrf/tasks", status_code=202)
def create_task(payload: WrfTaskCreate, request: Request) -> dict[str, Any]:
    key = request.headers.get("Idempotency-Key")
    if key and (len(key) > 128 or not all(c.isalnum() or c in "-_" for c in key)):
        raise HTTPException(status_code=422, detail="幂等键格式无效")
    hpc = task_manager.hpc_health
    if hpc.get("status") != "ready":
        hpc = task_manager.refresh_hpc_health()
    if hpc.get("status") != "ready":
        raise HTTPException(status_code=503, detail=f"tx-lab 环境未就绪：{hpc.get('message') or hpc.get('status')}")
    owner_sub, _ = _viewer(request)
    if not owner_sub:
        raise HTTPException(status_code=401, detail="Token 缺少用户身份")
    try:
        task = (task_manager.submit_idempotent(payload.model_dump(mode="json"), owner_sub, key)
                if key else task_manager.submit(payload.model_dump(mode="json"), owner_sub))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ok(task, message="WRF 任务已开始动态并行调度")


@app.post("/api/wrf/hpc/auth")
def authenticate_hpc(request: HpcAuthRequest) -> dict[str, Any]:
    if settings.hpc_connection_mode == "direct" and settings.hpc_auth_mode == "key":
        raise HTTPException(
            status_code=400,
            detail="tx-lab 工作台使用专用 SSH 密钥；密码只允许人工 SSH 登录，不由服务接收",
        )
    result = task_manager.authenticate_hpc(request.password.get_secret_value())
    if result.get("status") == "auth_required":
        stage = str(result.get("stage") or "密码认证")
        if stage == "计算节点密码认证":
            detail = "计算节点二次密码认证失败，请确认 4 号节点的 self 账号密码"
        else:
            detail = "堡垒机登录密码认证失败（尚未进入节点选择），请确认密码是否正确或已过期"
        # 这是远端超算凭据校验失败，不是当前智慧气象平台的 JWT 失效。
        # 返回 400，避免前端通用 401 处理错误地注销平台登录态。
        raise HTTPException(status_code=400, detail=detail)
    if result.get("status") != "ready":
        raise HTTPException(status_code=503, detail=f"tx-lab 连接或运行环境未就绪：{result.get('message') or result.get('status')}")
    return ok(result, message="tx-lab 认证成功")


@app.get("/api/wrf/tasks")
def list_tasks(request: Request, limit: int = 50) -> dict[str, Any]:
    owner_sub, role = _viewer(request)
    return ok({"items": task_manager.list(limit, None if role >= 3 else owner_sub), "queue_size": task_manager.queue_size})


@app.get("/api/wrf/tasks/{task_id}")
def task_detail(task_id: str, request: Request) -> dict[str, Any]:
    return ok(get_visible_task(task_id, request))


@app.delete("/api/wrf/tasks/{task_id}")
def delete_task(task_id: str, payload: TaskDeleteRequest, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        result = task_manager.delete_local(task_id, payload.confirm_task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok(result, message="WRF 本地任务数据已删除")


@app.get("/api/wrf/tasks/{task_id}/logs")
def task_logs(
    task_id: str,
    request: Request,
    after: int = 0,
    limit: int = 65536,
    attempt_no: int | None = None,
) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        return ok(task_manager.logs(task_id, after, limit, attempt_no))
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None


@app.post("/api/wrf/tasks/{task_id}/cancel")
def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        return ok(task_manager.cancel(task_id), message="取消请求已处理")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None


@app.post("/api/wrf/tasks/{task_id}/retry", status_code=202)
def retry_task(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        return ok(task_manager.retry(task_id), message="已在原任务中开始新的运行尝试")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/wrf/tasks/{task_id}/restart-plan")
def task_restart_plan(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        return ok(task_manager.restart_plan(task_id))
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/tasks/{task_id}/restart", status_code=202)
def restart_task(task_id: str, payload: WrfTaskRestartRequest, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        task = task_manager.restart(
            task_id,
            payload.request.model_dump(mode="json"),
            confirm_task_id=payload.confirm_task_id,
            confirm_attempt=payload.confirm_attempt,
        )
        return ok(task, message="已归档旧尝试并在原任务中重新开始")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/tasks/{task_id}/resume", status_code=202)
def resume_task(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        return ok(task_manager.resume_external(task_id), message="已从原任务阶段开始 tx-lab 对账")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/wrf/tasks/{task_id}/retry-outputs", status_code=202)
def retry_task_outputs(task_id: str, request: Request) -> dict[str, Any]:
    hpc = task_manager.hpc_health
    if hpc.get("status") != "ready":
        raise HTTPException(
            status_code=503,
            detail=f"tx-lab 环境未就绪：{hpc.get('message') or hpc.get('status')}，请先检查服务器连接",
        )
    try:
        get_visible_task(task_id, request)
        return ok(task_manager.retry_outputs(task_id), message="已开始结果下载恢复动态调度")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/wrf/tasks/{task_id}/render-partial", status_code=202)
def render_partial_outputs(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        return ok(task_manager.render_partial(task_id), message="已确认忽略不可读帧并开始部分渲染动态调度")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/wrf/tasks/{task_id}/publish", status_code=202)
def retry_catalog_publish(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_visible_task(task_id, request)
        task = task_manager.retry_catalog_publish(task_id)
        publication = ((task.get("result") or {}).get("catalog_publication") or {})
        if publication.get("status") == "failed":
            raise HTTPException(status_code=503, detail=publication)
        return ok(task, message="WRF 展示产品已登记到统一数据目录")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/wrf/tasks/{task_id}/result")
def task_result(task_id: str, request: Request) -> dict[str, Any]:
    task = get_visible_task(task_id, request)
    if task["status"] not in {"succeeded", "partial_success"} or not task.get("result"):
        raise HTTPException(status_code=409, detail={"message": "WRF 任务尚未完成", "status": task["status"]})
    return ok(task["result"])


@app.get("/api/wrf/tasks/{task_id}/summary")
def task_summary(task_id: str, request: Request) -> dict[str, Any]:
    task = get_visible_task(task_id, request)
    meta = ((task.get("result") or {}).get("meta_json") or {}) if task.get("status") in {"succeeded", "partial_success"} else {}
    domains = [{"id": domain.get("id"), "times": domain.get("times", []),
                "variables": [{"name": var.get("name"), "units": var.get("units"),
                               "frames": [{k: frame.get(k) for k in ("time", "min", "max", "mean")}
                                          for frame in var.get("frames", [])[:1000]]}
                              for var in domain.get("variables", [])]}
               for domain in meta.get("domains", [])]
    return ok({"task_id": task_id, "status": task.get("status"), "stage": task.get("stage"),
               "progress": task.get("progress"), "domains": domains,
               "quality_status": (meta.get("quality") or {}).get("status"),
               "catalog_status": ((task.get("result") or {}).get("catalog_publication") or {}).get("status"),
               "has_error": bool(task.get("error") or task.get("failure")),
               "failure_class": (task.get("failure") or {}).get("failure_class"),
               "message": "有异常，请在工作台查看任务日志" if task.get("error") or task.get("failure") else "任务状态已更新"})


@app.get("/api/wrf/display")
def display(request: Request, task_id: str | None = None) -> dict[str, Any]:
    owner_sub, role = _viewer(request)
    task = get_visible_task(task_id, request) if task_id else store.latest_success(None if role >= 3 else owner_sub)
    if task is None:
        return ok(None, message="暂无成功的 WRF 任务")
    if task["status"] not in {"succeeded", "partial_success"} or not task.get("result"):
        raise HTTPException(status_code=409, detail={"message": "指定 WRF 任务尚未完成", "status": task["status"]})
    return ok(task["result"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False, workers=1)
