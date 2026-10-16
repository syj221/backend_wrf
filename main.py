from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import uuid
import threading
import time
from typing import Any
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, JSONResponse

from config import settings
from auth import install_auth
from database import TaskStore
from gfs import GfsManager
from hpc import HpcClient, HpcError
from static_access import allow_run_asset
from legacy_display import corrected_display
from product_snapshot import ProductSnapshotError, product_identity, read_frame
from runtime_assets import RuntimeStaticFiles
from domain_preview import DomainPreviewRequest, preview_domains
from planning import PlanningAssessRequest, assess as assess_plan
from guidance import configuration_rules, RULESET
from recovery import RecoveryService
from request_validation import validate_ordinary_request
from runtime_release import readiness, require_ready, MpiConfigurationError
from namelist_config import catalog as namelist_catalog, resolve as resolve_namelist
from configuration_profile import definition as configuration_definition, normalize as normalize_common
from schemas import (
    PHYSICS_SUPPORTED_SCHEMES,
    FIXED_SPINUP_HOURS,
    HpcAuthRequest,
    RemoteGfsCleanupRequest,
    RemoteGfsTriggerRequest,
    TaskDeleteRequest,
    WrfTaskCreate,
    WrfTaskRestartRequest,
    normalize_task_request,
)
from task_deletion import TaskDeletionService
from forcing_policy import policy_update, request_download, cancel_download, observe_download
from forcing_cleanup import ForcingCleanupService
from task_manager import TaskConflictError, TaskNotFoundError, WrfTaskManager
from downscale_retired import RetiredDownscaleQuery, RetiredStateError, build_retired_router
from wrf_evidence_api import router as wrf_evidence_router


settings.ensure_directories()
store = TaskStore(settings.database_path)
gfs_manager = GfsManager(settings)
hpc_client = HpcClient(settings)
task_manager = WrfTaskManager(settings, store, gfs_manager, hpc_client)
retired_downscale_query = RetiredDownscaleQuery(settings.downscale_database_path)
deletion_service = TaskDeletionService(settings, store, hpc_client, task_manager._operation_lock)
forcing_cleanup_service = ForcingCleanupService(task_manager,deletion_service.queue)
task_manager.forcing_cleanup_service=forcing_cleanup_service
recovery_service = RecoveryService(task_manager, runtime_provider=lambda: namelist_runtime(timeout=12),
                                   runtime_verifier=lambda: namelist_runtime(force=True, timeout=20))
_guidance_pool_lock = threading.Lock()
_guidance_pool: dict[str, Any] = {"sampled_at": 0, "data": None}
_namelist_runtime: dict[str, Any] = {"sampled_at": 0, "data": None}
_namelist_lock = threading.Lock()


def namelist_runtime(force=False, timeout=45):
    import shlex
    from pathlib import Path
    if not _namelist_lock.acquire(timeout=1 if timeout < 45 else timeout):
        return None
    try:
        if force or time.time() - _namelist_runtime["sampled_at"] > 300:
            try:
                script = (Path(__file__).parent / "scripts/namelist_source_probe.py").read_text()
                raw = hpc_client.run(shlex.quote(settings.hpc_runtime_python) + " -B -c " + shlex.quote(script) + " " + shlex.quote(settings.hpc_wrf_source_dir) + " " + shlex.quote(settings.hpc_wps_source_dir) + " " + shlex.quote(hpc_client.service_dir()), timeout=timeout)
                evidence = json.loads(raw)
                _namelist_runtime.update(data={k: evidence.get(k) for k in ("source_hash", "binary_hashes", "generator_hashes")}, sampled_at=time.time())
            except Exception:
                _namelist_runtime.update(data=None, sampled_at=time.time())
        return _namelist_runtime["data"]
    finally:
        _namelist_lock.release()


class RecoveryPlanRequest(BaseModel):
    event_id: str | None = None
    purpose: Literal["repair", "manual"] = "repair"
    action: Literal["resume", "retry_outputs", "render_partial", "restart", "reduce_time_step", "regenerate_plan"]
    request: WrfTaskCreate | None = None


class RecoveryConfirmation(BaseModel):
    confirmed: Literal[True]


def allow_wrf_asset(path: str, user: dict[str, Any]) -> bool:
    """Only task owners may read unpublished run assets."""
    def lookup(task_id: str) -> dict[str, Any] | None:
        if task_id.startswith("ds_"):
            return downscale_asset_owner(task_id)
        try:
            return task_manager.get(task_id)
        except TaskNotFoundError:
            return None

    return allow_run_asset(path, user, lookup)


def downscale_asset_owner(task_id: str) -> dict[str, Any] | None:
    try:
        return retired_downscale_query.lookup(task_id)
    except RetiredStateError:
        # Unreadable ownership evidence never grants access to a legacy asset.
        return None












@asynccontextmanager
async def lifespan(_app: FastAPI):
    task_manager.start()
    try:
        yield
    finally:
        task_manager.stop()
        deletion_service.close()
        forcing_cleanup_service.close()
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
        ("/api/wrf/tasks", {"GET": 1, "POST": 2, "DELETE": 2, "PATCH": 2}),
        ("/api/wrf/submissions", {"GET": 1}),
        ("/api/wrf/namelist", {"GET": 1, "POST": 1}),
        # Retired queries remain authenticated; no legacy writes are dispatched.
        ("/api/wrf/downscale", {"GET": 1, "POST": 1, "DELETE": 1}),
        ("/api/wrf/workbench/tasks", {"GET": 1}),
        ("/api/wrf/data-status", {"GET": 1, "*": 2}),
        ("/api/wrf/gfs", {"GET": 1, "*": 2}),
        ("/api/wrf/forcing", {"GET": 1, "*": 2}),
        ("/api/wrf/recommendations", {"GET": 1, "*": 2}),
        ("/api/wrf/evidence", {"GET": 2, "POST": 2}),
        ("/api/wrf/options", {"GET": 1, "*": 2}),
        ("/api/wrf/readiness", {"GET": 1, "*": 3}),
        ("/api/wrf/planning/assess", {"POST": 1}),
        ("/api/wrf/domains/preview", {"POST": 1}),
        ("/api/wrf/hpc", 2),
        ("/api/wrf/display", 1),
        ("/data/WRF/", 1),
    ],
    asset_authorizer=allow_wrf_asset,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/data/WRF", RuntimeStaticFiles(settings, lambda task_id: store.get(task_id) if task_id.startswith("wrf_") else downscale_asset_owner(task_id)), name="wrf-data")


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
    archived = None if request.query_params.get("include_archived") == "true" else request.query_params.get("archived", "false").lower() == "true"
    page = store.page(limit, None if role >= 3 else owner_sub, archived=archived,
                      **({"cursor":request.query_params["cursor"]} if request.query_params.get("cursor") else {}))
    if request.query_params.get("paginated") == "true":
        return {**page,"active_items":[task for task in store.active()
            if role >= 3 or task.get("owner_sub") == owner_sub]}
    return page["items"]


app.include_router(build_retired_router(retired_downscale_query, _viewer, _normal_visible_tasks))
app.include_router(wrf_evidence_router)


@app.middleware("http")
async def retired_features(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/wrf/recommendations"):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=410, content={"detail": "该功能已退役，请使用普通 WRF 工作台。"})
    return await call_next(request)


@app.get("/api/wrf/readiness")
def runtime_readiness(refresh: bool = False):
    return ok(readiness(hpc_client, settings, force=refresh))


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


@app.get("/api/health/activity")
def local_activity() -> dict[str, Any]:
    """Local shutdown guard only; never refresh SSH or expose private task IDs."""
    return ok({
        "schema": "wrf-local-activity-1",
        "active_task_count": task_manager.active_task_count,
        "queue_size": task_manager.queue_size,
        "downscale_active": retired_downscale_query.local_activity()["downscale_active"],
    })


@app.get("/api/health")
def health(cached: bool = False) -> dict[str, Any]:
    hpc_health = task_manager.hpc_health
    if not cached and hpc_health.get("status") != "ready":
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
            "max_concurrent_tasks": settings.ordinary_max_concurrent,
            "resource_policy": {"cpu_headroom": settings.compute_cpu_headroom,
                                "memory_headroom_bytes": settings.compute_memory_headroom_bytes,
                                "reservation": "per_task_cpu_memory"},
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
                "retired": True,
                "runner_enabled": False,
                "active_task_id": None,
                "stage_boundary": "retired_readonly",
            },
        }
    )


@app.get("/api/wrf/options")
def options() -> dict[str, Any]:
    return ok(
        {
            "capabilities": {
                "data_sources": ["GFS", "ECMWF"],
                "configuration_schema": "wrf-composer-2",
                "execution_modes": ["TX_LAB"],
                "max_domains": 4,
                "scheduling_mode": "persistent_serial_queue",
                "runtime_profiles": ["cpu"],
                "default_runtime_profile": "cpu",
                "default_spinup_hours": 6,
                "default_vertical_levels": 88,
                "vertical_levels_range": [10, 500],
                "fixed_forecast_focus": "general",
                "gfs_cycle_hours": [0],
                "gfs_cycle_policy": "latest_covering_00z",
                "gfs_product": "gfs-0p25-global",
                "gfs_scope": "global",
                "gfs_bounds": hpc_client.gfs_bounds,
                "gfs_download_mode": "hpc_remote_nomads_grib_filter",
                "ecmwf_cycle_hours": [0],
                "ecmwf_cycle_policy": "latest_covering_00z",
                "ecmwf_product": "ecmwf-ifs-0p25",
                "ecmwf_download_mode": "tx_lab_remote_full_file",
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
                "downscale_retired": True,
            },
            "forecast_intervals": [1, 3, 6],
            "configuration_profile": configuration_definition(namelist_catalog()["capability_id"]),
            "history_intervals_minutes": [10, 30, 60, 180, 360],
            "capability_version": "wrf-ordinary-2",
            "workbench_management": {"id": "wrf-workbench-management-1",
                "interaction_revision":"workbench-simplification-3", "async_deletion":True, "include_archived":True,
                "direct_deletion":True,"forcing_refresh":True,"forcing_cycle_deletion":True,"directory_quarantine":True},
            "recovery_capability": {"schema_version": "wrf-diagnosis-1", "plan_version": "wrf-recovery-2",
                                    "diagnosis": True, "regenerate_plan": True},
            "namelist_capability": {k: namelist_catalog()[k] for k in ("schema_version", "source_hash", "capability_id", "wrf_version", "wps_version")},
            "field_descriptions": {
                "forecast_interval_hours": "边界资料周期（小时），不是产品输出间隔。",
                "history_interval_minutes": "模拟产品输出周期（分钟），全域统一。",
                "center": "计算域参考点；明确坐标可直接编辑，提交时校验投影边界。",
                "domains": "d01–d04 嵌套域；子域网格距与父域比为 3 或 5。",
            },
            "assimilation_schemes": [
                {"value": "off", "label": "关闭"},
                {"value": "fdda_weak", "label": "弱网格松弛"},
                {"value": "fdda_standard", "label": "标准网格松弛"},
                {"value": "fdda_strong", "label": "强网格松弛"},
            ],
            "physics_options": {name: sorted(values) for name, values in PHYSICS_SUPPORTED_SCHEMES.items()},
            "physics_presets": {
                "默认通用": {"mp_physics": 8, "cu_physics": 3, "ra_lw_physics": 4, "ra_sw_physics": 4, "bl_pbl_physics": 1, "sf_sfclay_physics": 1, "sf_surface_physics": 2, "sf_urban_physics": 0, "num_soil_layers": 4, "num_land_cat": 21, "radt": 5},
            },
            "default_domain_count": 0,
            "default_domains": [],
        }
    )


@app.get("/api/wrf/data-status")
def data_status() -> dict[str, Any]:
    try:
        pool_items: list[dict[str, Any]] = []
        status_priority = {
            "failed": 6, "error": 6, "rate_limited": 5, "downloading": 4,
            "retry_wait": 5, "queued": 3, "waiting_publish": 2, "partial": 1, "ready": 1, "idle": 0,
        }
        overall = "idle"
        hpc_health = task_manager.hpc_health
        for source in ("gfs", "ecmwf"):
            target_cycles = gfs_manager.latest_cycles(
                count=settings.hpc_gfs_retained_cycles,
                data_source=source,
            )
            active_cycles = task_manager.active_forcing_cycles(source)
            reference_counts = task_manager.task_forcing_reference_counts(source)
            download_counts = store.forcing_download_reference_counts(source)
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
                    cycle["task_required"] = reference_counts.get(key, 0) > 0
                    cycle["task_reference_count"] = reference_counts.get(key, 0)
                    cycle["download_reference_count"] = download_counts.get(key, 0)
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


@app.post("/api/wrf/planning/assess")
def planning_assess(payload: PlanningAssessRequest) -> dict[str, Any]:
    with _guidance_pool_lock:
        if time.time() - _guidance_pool["sampled_at"] > 60:
            _guidance_pool.update(sampled_at=time.time(), data=data_status()["data"])
        pool = {**(_guidance_pool["data"] or {}), "sampled_at": _guidance_pool["sampled_at"]}
    return ok(assess_plan(payload, {**task_manager.hpc_health, "queue_size": task_manager.queue_size,
        "active_task_count": task_manager.active_task_count}, pool))


@app.get("/api/wrf/namelist/catalog")
def get_namelist_catalog():
    return ok(namelist_catalog())


@app.post("/api/wrf/namelist/resolve")
def namelist_resolve(payload: dict[str, Any]):
    try:
        return ok(resolve_namelist(payload, strict=False))
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/wrf/domains/preview")
def domains_preview(payload: DomainPreviewRequest) -> dict[str, Any]:
    try:
        return ok(preview_domains(payload))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/wrf/forcing/prepare", status_code=202)
def prepare_all_forcing() -> dict[str, Any]:
    """Check GFS and ECMWF pools and start missing forecast downloads."""
    try:
        gfs = task_manager.sync_latest_gfs(cleanup_old_cycles=False)
        ec = sync_latest_remote_forcing("ecmwf").get("data") or {}
        return ok({"providers": {"gfs": gfs, "ecmwf": ec}})
    except (ValueError, HpcError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/forcing/refresh", status_code=202)
def refresh_forcing_pool(request: Request):
    actor, role = _viewer(request)
    return ok(forcing_cleanup_service.refresh_async(actor,role))


@app.get("/api/wrf/forcing/refresh")
def forcing_refresh_status(request: Request):
    _viewer(request)
    return ok(forcing_cleanup_service.refresh_status())


@app.post("/api/wrf/forcing/{source}/cycles/{cycle}/delete",status_code=202)
def delete_forcing_cycle(source: str, cycle: str, request: Request):
    actor,role=_viewer(request)
    if role<3:
        raise HTTPException(403,"共享资料删除仅管理员可操作")
    try:
        return ok(forcing_cleanup_service.delete_cycle_async(source,cycle,actor))
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None


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


class ForcingPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)
    enabled: bool
    auto_cleanup: bool
    held_cycles: dict[str, list[str]]


class ForcingDownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: Literal["gfs", "ecmwf"]
    cycle: str = Field(min_length=10, max_length=10)
    start_hour: int = Field(ge=0, le=72)
    end_hour: int = Field(ge=0, le=72)


@app.get("/api/wrf/forcing/policy")
def get_forcing_policy():
    return ok(store.forcing_policy(settings.hpc_gfs_prefetch_enabled))


@app.patch("/api/wrf/forcing/policy")
def patch_forcing_policy(payload: ForcingPolicyPatch, request: Request):
    if _viewer(request)[1] < 3:
        raise HTTPException(403, "资料策略仅管理员可修改")
    try:
        return ok(policy_update(store, task_manager, hpc_client, settings, **payload.model_dump()))
    except (ValueError, HpcError) as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/wrf/forcing/downloads", status_code=202)
def submit_forcing_download(payload: ForcingDownloadRequest, request: Request):
    actor, _ = _viewer(request)
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(422,"资料下载需要 Idempotency-Key")
    try:
        return ok(request_download(store,hpc_client,actor,payload.source,payload.cycle,payload.start_hour,payload.end_hour,key=key))
    except (ValueError,HpcError) as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/wrf/forcing/downloads/{operation_id}")
def get_forcing_download(operation_id: str, request: Request):
    operation=store.forcing_operation(operation_id)
    actor,role=_viewer(request)
    if not operation or (role<3 and operation["owner_sub"]!=actor):
        raise HTTPException(404,"下载操作不存在")
    return ok(observe_download(store,hpc_client,operation))


@app.get("/api/wrf/forcing/downloads")
def list_forcing_downloads(request: Request, limit: int = Query(default=50,ge=1,le=100)):
    actor, role = _viewer(request)
    return ok({"items":store.list_forcing_operations(None if role>=3 else actor,limit)})


@app.post("/api/wrf/forcing/downloads/{operation_id}/cancel")
def cancel_forcing_download(operation_id: str, request: Request):
    operation=store.forcing_operation(operation_id)
    actor,role=_viewer(request)
    if not operation or (role<3 and operation["owner_sub"]!=actor):
        raise HTTPException(404,"下载操作不存在")
    try:
        result = cancel_download(store,hpc_client,operation)
        task_manager._forcing_cleanup_wakeup.set()
        return ok(result)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None


@app.get("/api/wrf/forcing/{data_source}/cleanup-preview")
def forcing_cleanup_preview(data_source: str, request: Request):
    if _viewer(request)[1]<3:
        raise HTTPException(403,"资料清理仅管理员可预览")
    return ok(forcing_cleanup_service.preview(data_source))


class ForcingCleanupConfirmation(BaseModel):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


@app.post("/api/wrf/forcing/{data_source}/cleanup-plans")
def create_forcing_cleanup_plan(data_source: str, request: Request, retention_upgrade: bool = False):
    actor, role = _viewer(request)
    if role < 3:
        raise HTTPException(403,"资料清理仅管理员可操作")
    try:
        return ok(forcing_cleanup_service.plan(data_source,actor,retention_upgrade))
    except (ValueError,HpcError) as exc:
        raise HTTPException(409,str(exc)) from None


@app.get("/api/wrf/forcing/cleanup-plans/{plan_id}")
def get_forcing_cleanup_plan(plan_id: str, request: Request):
    actor, role = _viewer(request)
    if role < 3:
        raise HTTPException(403,"资料清理仅管理员可操作")
    try:
        return ok(forcing_cleanup_service.status(plan_id,actor))
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from None


@app.post("/api/wrf/forcing/cleanup-plans/{plan_id}/execute")
def execute_forcing_cleanup_plan(plan_id: str, payload: ForcingCleanupConfirmation, request: Request):
    actor, role = _viewer(request)
    if role < 3:
        raise HTTPException(403,"资料清理仅管理员可操作")
    try:
        result=forcing_cleanup_service.execute(plan_id,actor,payload.digest)
        return JSONResponse(status_code=202 if result["status"] in {"checking","executing"} else 200,content=ok(result))
    except (ValueError,HpcError) as exc:
        raise HTTPException(409,str(exc)) from None


@app.post("/api/wrf/forcing/{data_source}/cleanup")
def cleanup_remote_forcing(data_source: str, request: RemoteGfsCleanupRequest, http_request: Request) -> dict[str, Any]:
    if _viewer(http_request)[1] < 3:
        raise HTTPException(status_code=403, detail="共享资料清理仅限管理员")
    raise HTTPException(410,"直接资料删除已停用；请创建并确认资料清理计划")


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
def cleanup_remote_gfs(request: RemoteGfsCleanupRequest, http_request: Request) -> dict[str, Any]:
    if _viewer(http_request)[1] < 3:
        raise HTTPException(status_code=403, detail="共享资料清理仅限管理员")
    raise HTTPException(410,"直接资料删除已停用；请创建并确认资料清理计划")


@app.post("/api/wrf/gfs/trigger", status_code=202)
def trigger_remote_gfs(request: RemoteGfsTriggerRequest) -> dict[str, Any]:
    try:
        result = hpc_client.trigger_gfs_download(request.cycle, 72)
        result["auto_cleanup"] = {"requested": [], "deleted": [], "missing": []}
        return ok(result, message="GFS 下载请求已检查；资料清理须单独执行")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None






@app.post("/api/wrf/tasks/validate")
def validate_task(payload: WrfTaskCreate) -> dict[str, Any]:
    try:
        result = validate_ordinary_request(payload.model_dump(mode="json"), runtime=namelist_runtime()
            if payload.namelist is not None or payload.configuration_profile else None,
            original_request=payload._provided_request)
    except ValueError as exc:
        raise HTTPException(409 if "无法确认当前 WRF/WPS" in str(exc) else 422, str(exc)) from exc
    return ok(result)


@app.post("/api/wrf/tasks", status_code=202)
def create_task(payload: WrfTaskCreate, request: Request) -> dict[str, Any]:
    canonical_request = payload.model_dump(mode="json")
    canonical_request = validate_task(payload)["data"]["request"]
    key = request.headers.get("Idempotency-Key")
    if key and (len(key) > 128 or not all(c.isalnum() or c in "-_" for c in key)):
        raise HTTPException(status_code=422, detail="幂等键格式无效")
    try:
        require_ready(hpc_client, settings, force=True, request=canonical_request)
    except MpiConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    owner_sub, _ = _viewer(request)
    if not owner_sub:
        raise HTTPException(status_code=401, detail="Token 缺少用户身份")
    try:
        task = (task_manager.submit_idempotent(canonical_request, owner_sub, key)
                if key else task_manager.submit(canonical_request, owner_sub))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ok(task, message="WRF 任务已进入持久化队列")


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


class TaskMetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)
    name: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=4000)
    archived: bool = False


@app.get("/api/wrf/tasks")
def list_tasks(request: Request, limit: int = Query(default=50, ge=1, le=200),
               search: str = Query(default="", max_length=200),
               source: Literal["gfs", "ecmwf"] | None = None, status: str | None = None,
               archived: bool = False, cursor: str | None = Query(default=None, max_length=1024)) -> dict[str, Any]:
    owner_sub, role = _viewer(request)
    try:
        page = store.page(limit, None if role >= 3 else owner_sub, search, source, status, archived, cursor)
        page["items"] = [{**task_manager.get(item["id"]), "metadata": item["metadata"],
                          "operations": task_manager.operations(item["id"])} for item in page["items"]]
        return ok({**page, "queue_size": task_manager.queue_size})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.patch("/api/wrf/tasks/{task_id}/metadata")
def task_metadata(task_id: str, payload: TaskMetadataPatch, request: Request):
    get_visible_task(task_id, request)
    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    try:
        return ok(store.patch_metadata(task_id, payload.version, changes))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/wrf/submissions/{key}")
def submission_status(key: str, request: Request) -> dict[str, Any]:
    """Read-only owner-bound reconciliation; never dispatches a WRF task."""
    if not key or len(key) > 128 or not all(c.isalnum() or c in "-_" for c in key):
        raise HTTPException(422, "幂等键格式无效")
    owner_sub, _ = _viewer(request)
    if not owner_sub:
        raise HTTPException(401, "Token 缺少用户身份")
    reservation = store.get_submission(owner_sub, key)
    if reservation is None:
        return ok({"status": "not_found", "task_id": None})
    task = store.get(reservation["task_id"])
    if task and task.get("owner_sub") == owner_sub:
        return ok({"status": "accepted" if reservation["accepted"] else "reserved",
                   "task_id": task["id"], "task_status": task["status"]})
    return ok({"status": "task_deleted" if reservation["accepted"] else "reserved", "task_id": None})


@app.get("/api/wrf/tasks/{task_id}")
def task_detail(task_id: str, request: Request) -> dict[str, Any]:
    return ok({**get_visible_task(task_id, request), "metadata": store.metadata(task_id),
               "operations": task_manager.operations(task_id)})


@app.delete("/api/wrf/tasks/{task_id}")
def delete_task(task_id: str, payload: TaskDeleteRequest, request: Request) -> dict[str, Any]:
    get_visible_task(task_id, request)
    raise HTTPException(410, "旧本地删除入口已停用；彻底删除必须通过精确清单事务，当前请使用归档")


class TaskDeletionConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmed: Literal[True]
    digest: str = Field(min_length=64, max_length=64)


@app.post("/api/wrf/tasks/{task_id}/delete", status_code=202)
def task_delete_direct(task_id: str, request: Request):
    get_visible_task(task_id,request)
    actor, _ = _viewer(request)
    bearer = request.headers.get("authorization", "")
    if not bearer.startswith("Bearer "):
        raise HTTPException(401,"请重新登录后删除任务")
    try:
        return ok(deletion_service.plan_async(task_id,actor,delete_now=True,bearer=bearer[7:]))
    except (ValueError,RuntimeError,HpcError):
        raise HTTPException(409,"任务暂不能删除，请稍后重试。") from None


@app.post("/api/wrf/tasks/{task_id}/deletion-plans")
def task_deletion_plan(task_id: str, request: Request, asynchronous: bool = False):
    get_visible_task(task_id, request)
    actor, _ = _viewer(request)
    try:
        if asynchronous:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=202, content=ok(deletion_service.plan_async(task_id, actor)))
        return ok(deletion_service.plan(task_id, actor))
    except (ValueError, RuntimeError, HpcError) as exc:
        operation_id = uuid.uuid4().hex
        logging.getLogger(__name__).exception("WRF deletion preview %s failed", operation_id)
        raise HTTPException(409, {"code": "WRF_TERMINAL_EVIDENCE_UNVERIFIED",
            "message": "无法核验任务资产或计算会话，请重新核验；未执行删除。",
            "operation_id": operation_id}) from None


@app.get("/api/wrf/tasks/{task_id}/deletion-plans/{plan_id}")
def task_deletion_status(task_id: str, plan_id: str, request: Request):
    actor, _ = _viewer(request)
    try:
        return ok(deletion_service.status(task_id, plan_id, actor))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/wrf/tasks/{task_id}/deletion-plans/{plan_id}/execute")
def task_deletion_execute(task_id: str, plan_id: str, payload: TaskDeletionConfirmation, request: Request, asynchronous: bool = False):
    get_visible_task(task_id, request)
    actor, _ = _viewer(request)
    bearer = request.headers.get("authorization", "")
    if not bearer.startswith("Bearer "):
        raise HTTPException(401, "请重新登录后执行删除")
    try:
        with task_manager._operation_lock(task_id):
            if asynchronous:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=202, content=ok(deletion_service.execute_async(task_id, plan_id, actor, payload.digest, bearer.removeprefix("Bearer "))))
            return ok(deletion_service.execute(task_id, plan_id, actor, payload.digest, bearer.removeprefix("Bearer ")))
    except (ValueError, RuntimeError, HpcError) as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/wrf/tasks/{task_id}/deletion-plans/{plan_id}/reconcile")
def task_deletion_reconcile(task_id: str, plan_id: str, payload: RecoveryConfirmation, request: Request):
    actor, _ = _viewer(request)
    bearer = request.headers.get("authorization", "")
    if not bearer.startswith("Bearer "):
        raise HTTPException(401, "请重新登录后对账")
    try:
        with task_manager._operation_lock(task_id):
            result=deletion_service.reconcile(task_id, plan_id, actor, bearer.removeprefix("Bearer "))
            return JSONResponse(status_code=202 if result["status"] in {"checking","executing"} else 200,content=ok(result))
    except (ValueError, RuntimeError, HpcError) as exc:
        raise HTTPException(409, str(exc)) from None


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


@app.get("/api/wrf/tasks/{task_id}/events")
def task_events(task_id: str, request: Request):
    get_visible_task(task_id, request)
    return ok(recovery_service.events(task_id))


@app.get("/api/wrf/tasks/{task_id}/diagnosis")
def task_diagnosis(task_id: str, request: Request, event_id: str | None = None,
                   attempt_no: int | None = Query(default=None, ge=1), refresh: bool = False):
    get_visible_task(task_id, request)
    try:
        if refresh:
            return ok(recovery_service.diagnosis(task_id, event_id, attempt_no,
                runtime=namelist_runtime(force=True, timeout=20), force_refresh=True))
        return ok(recovery_service.diagnosis(task_id, event_id, attempt_no))
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/wrf/tasks/{task_id}/recovery-plans")
def task_recovery_plan(task_id: str, payload: RecoveryPlanRequest, request: Request):
    get_visible_task(task_id, request)
    try:
        return ok(recovery_service.plan(task_id, payload.event_id, payload.action,
                                       payload.request.model_dump(mode="json") if payload.request else None,
                                       actor_sub=_viewer(request)[0], purpose=payload.purpose))
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(409, str(exc)) from None
    except (HpcError, RuntimeError) as exc:
        operation_id = uuid.uuid4().hex
        logging.getLogger(__name__).exception("WRF recovery operation %s failed", operation_id)
        raise HTTPException(409, {"code": "recovery_evidence_unavailable",
            "message": "运行证据暂不可核验，请稍后重新检查；未自动重放操作",
            "operation_id": operation_id}) from None


@app.get("/api/wrf/tasks/{task_id}/recovery-plans/{plan_id}")
def task_recovery_detail(task_id: str, plan_id: str, request: Request):
    get_visible_task(task_id, request)
    plan = store.recovery(plan_id)
    if not plan or plan["task_id"] != task_id:
        raise HTTPException(404, "恢复计划不存在")
    return ok(plan)


@app.post("/api/wrf/tasks/{task_id}/recovery-plans/{plan_id}/execute")
def task_recovery_execute(task_id: str, plan_id: str, payload: RecoveryConfirmation, request: Request):
    get_visible_task(task_id, request)
    try:
        return ok(recovery_service.execute(task_id, plan_id, actor_sub=_viewer(request)[0]))
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(409, str(exc)) from None
    except (HpcError, RuntimeError) as exc:
        operation_id = uuid.uuid4().hex
        logging.getLogger(__name__).exception("WRF recovery operation %s failed", operation_id)
        raise HTTPException(409, {"code": "recovery_evidence_unavailable",
            "message": "运行证据暂不可核验，请稍后重新检查；未自动重放操作",
            "operation_id": operation_id}) from None


def get_legacy_recovery_task(task_id: str, request: Request):
    task = get_visible_task(task_id, request)
    if task.get("request", {}).get("recovery_policy") == "confirm":
        raise HTTPException(409, "此任务须先生成操作计划：工作台确认执行，智能体通过原生 ASK 确认；旧接口不可绕过确认")
    return task


@app.post("/api/wrf/tasks/{task_id}/retry", status_code=202)
def retry_task(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_legacy_recovery_task(task_id, request)
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
        get_legacy_recovery_task(task_id, request)
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
        get_legacy_recovery_task(task_id, request)
        return ok(task_manager.resume_external(task_id), message="已从原任务阶段开始 tx-lab 对账")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/wrf/tasks/{task_id}/retry-outputs", status_code=202)
def retry_task_outputs(task_id: str, request: Request) -> dict[str, Any]:
    get_legacy_recovery_task(task_id, request)
    hpc = task_manager.hpc_health
    if hpc.get("status") != "ready":
        raise HTTPException(
            status_code=503,
            detail=f"tx-lab 环境未就绪：{hpc.get('message') or hpc.get('status')}，请先检查服务器连接",
        )
    try:
        return ok(task_manager.retry_outputs(task_id), message="已开始结果下载恢复动态调度")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/wrf/tasks/{task_id}/render-partial", status_code=202)
def render_partial_outputs(task_id: str, request: Request) -> dict[str, Any]:
    try:
        get_legacy_recovery_task(task_id, request)
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
    try:
        return ok(corrected_display(task, settings))
    except ProductSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
               **product_identity(task),
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
    try:
        return ok(corrected_display(task, settings))
    except ProductSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/wrf/tasks/{task_id}/assets")
def task_frame(task_id: str, request: Request, path: str = Query(min_length=1, max_length=2048),
               attempt_no: int = Query(ge=1),
               product_version: str = Query(pattern=r"^[0-9a-f]{64}$")):
    task = get_visible_task(task_id, request)
    try:
        current = corrected_display(task, settings)
        content = read_frame(task, current, settings.output_dir, path, attempt_no, product_version,
                             lambda identity: get_visible_task(identity, request), settings=settings)
    except ProductSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError:
        raise HTTPException(status_code=404, detail="产品不存在或不可访问") from None
    return Response(content, media_type="image/webp", headers={
        "Cache-Control": "private, no-store", "ETag": '"' + product_version + '"',
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False, workers=1)
