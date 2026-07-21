from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from auth import install_auth
from config import settings
from database import TaskStore
from gfs import GfsManager
from hpc import HpcClient, HpcError
from recommendations import RecommendationManager
from schemas import HpcAuthRequest, RemoteGfsCleanupRequest, RemoteGfsTriggerRequest, TaskDeleteRequest, WrfRecommendationRequest, WrfTaskCreate
from task_manager import TaskConflictError, TaskNotFoundError, WrfTaskManager


settings.ensure_directories()
store = TaskStore(settings.database_path)
gfs_manager = GfsManager(settings)
hpc_client = HpcClient(settings)
task_manager = WrfTaskManager(settings, store, gfs_manager, hpc_client)
recommendation_manager = RecommendationManager(settings, hpc_client)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task_manager.start()
    try:
        yield
    finally:
        task_manager.stop()
        recommendation_manager.stop()
        store.close()


app = FastAPI(
    title="Weather WRF Backend",
    description="超算共享 GFS 数据池调度、WPS/WRF 执行与 WebP 结果发布微服务。",
    version="1.0.0",
    lifespan=lifespan,
)

install_auth(
    app,
    [
        ("/api/wrf/tasks", 2),
        ("/api/wrf/data-status", 2),
        ("/api/wrf/gfs", 2),
        ("/api/wrf/recommendations", 2),
        ("/api/wrf/options", 2),
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


@app.get("/")
def root() -> dict[str, Any]:
    return ok({"service": "backend_wrf", "docs": "/docs"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    hpc_health = task_manager.hpc_health
    hpc_health["transfer"] = hpc_client.transfer_status
    hpc_health["server_index"] = settings.hpc_server_index
    hpc_health["account_index"] = settings.hpc_account_index
    return ok(
        {
            "status": "online",
            "mode": "parallel_hpc_task_service",
            "queue_size": task_manager.queue_size,
            "active_task_id": task_manager.active_task_id,
            "active_task_ids": task_manager.active_task_ids,
            "active_task_count": task_manager.active_task_count,
            "max_concurrent_tasks": settings.max_concurrent_tasks,
            "hpc": hpc_health,
            "gfs": {"mode": "hpc_remote_pool"},
        }
    )


@app.get("/api/wrf/options")
def options() -> dict[str, Any]:
    return ok(
        {
            "capabilities": {
                "data_sources": ["GFS"],
                "execution_modes": ["HPC"],
                "max_domains": 4,
                "max_concurrent_tasks": settings.max_concurrent_tasks,
                "gfs_cycle_hours": [0],
                "gfs_cycle_policy": "latest_covering_00z",
                "gfs_product": "gfs-0p25-full",
                "gfs_download_mode": "hpc_remote_full_file",
                "hpc_transfer_mode": settings.hpc_transfer_mode,
                "hpc_import_legacy_gfs": settings.hpc_import_legacy_gfs,
            },
            "forecast_intervals": [1, 3, 6, 12, 24],
            "forecast_focuses": [
                {"value": "general", "label": "通用预报"},
                {"value": "convection", "label": "强对流 / 降水"},
                {"value": "temperature_wind", "label": "温度 / 风场"},
                {"value": "urban", "label": "城市精细预报"},
                {"value": "snowfall", "label": "降雪过程"},
            ],
            "spinup_hours": [0, 3, 6, 12, 18, 24],
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
        target_cycles = gfs_manager.latest_cycles()
        pool_items = hpc_client.gfs_pool_items(target_cycles, task_manager.active_gfs_cycles())
        return ok({
            "status": pool_items[0].get("status", "idle"),
            "mode": "hpc_remote_pool",
            "target_cycles": target_cycles,
            "pool_items": pool_items,
        })
    except Exception as exc:
        return ok({"status": "unavailable", "mode": "hpc_remote_pool", "message": str(exc), "pool_items": []})


@app.post("/api/wrf/gfs/sync-latest", status_code=202)
def sync_latest_remote_gfs() -> dict[str, Any]:
    target_cycles = gfs_manager.latest_cycles()
    try:
        pool = hpc_client.gfs_pool_items(target_cycles, task_manager.active_gfs_cycles())[0]
        by_cycle = {item["cycle"]: item for item in pool.get("cycles") or []}
        actions = []
        for cycle in reversed(target_cycles):
            item = by_cycle.get(cycle) or {}
            if item.get("complete"):
                actions.append({"cycle": cycle, "status": "ready", "detail": "73/73"})
            else:
                actions.append(hpc_client.trigger_gfs_download(cycle, 72))
        return ok({"target_cycles": target_cycles, "actions": actions}, message="超算最近两个 00Z 周期同步状态已检查")
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
            task_manager.active_gfs_cycles(),
        )
        return ok(result, message="已清理确认的超算旧 GFS 周期")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/gfs/trigger", status_code=202)
def trigger_remote_gfs(request: RemoteGfsTriggerRequest) -> dict[str, Any]:
    try:
        return ok(hpc_client.trigger_gfs_download(request.cycle, 72), message="超算共享 GFS 下载状态已检查")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HpcError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@app.post("/api/wrf/recommendations", status_code=202)
def create_recommendation(request: WrfRecommendationRequest) -> dict[str, Any]:
    return ok(
        recommendation_manager.submit(request.model_dump(mode="json")),
        message="已提交超算 geogrid 地理分析",
    )


@app.get("/api/wrf/recommendations/{job_id}")
def recommendation_detail(job_id: str) -> dict[str, Any]:
    job = recommendation_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="WRF 参数推荐任务不存在")
    return ok(job)


@app.post("/api/wrf/tasks", status_code=202)
def create_task(request: WrfTaskCreate) -> dict[str, Any]:
    hpc = task_manager.hpc_health
    if hpc.get("status") != "ready":
        hpc = task_manager.refresh_hpc_health()
    if hpc.get("status") != "ready":
        raise HTTPException(status_code=503, detail=f"超算环境未就绪：{hpc.get('message') or hpc.get('status')}")
    task = task_manager.submit(request.model_dump(mode="json"))
    return ok(task, message="WRF 任务已进入并行队列")


@app.post("/api/wrf/hpc/auth")
def authenticate_hpc(request: HpcAuthRequest) -> dict[str, Any]:
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
        raise HTTPException(status_code=503, detail=f"超算连接或运行环境未就绪：{result.get('message') or result.get('status')}")
    return ok(result, message="超算认证成功")


@app.get("/api/wrf/tasks")
def list_tasks(limit: int = 50) -> dict[str, Any]:
    return ok({"items": task_manager.list(limit), "queue_size": task_manager.queue_size})


@app.get("/api/wrf/tasks/{task_id}")
def task_detail(task_id: str) -> dict[str, Any]:
    return ok(get_task(task_id))


@app.delete("/api/wrf/tasks/{task_id}")
def delete_task(task_id: str, request: TaskDeleteRequest) -> dict[str, Any]:
    try:
        result = task_manager.delete_local(task_id, request.confirm_task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ok(result, message="WRF 本地任务数据已删除")


@app.get("/api/wrf/tasks/{task_id}/logs")
def task_logs(task_id: str, after: int = 0, limit: int = 65536) -> dict[str, Any]:
    try:
        return ok(task_manager.logs(task_id, after, limit))
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None


@app.post("/api/wrf/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    try:
        return ok(task_manager.cancel(task_id), message="取消请求已处理")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None


@app.post("/api/wrf/tasks/{task_id}/retry", status_code=202)
def retry_task(task_id: str) -> dict[str, Any]:
    try:
        return ok(task_manager.retry(task_id), message="已基于原参数创建新任务")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None


@app.post("/api/wrf/tasks/{task_id}/retry-outputs", status_code=202)
def retry_task_outputs(task_id: str) -> dict[str, Any]:
    hpc = task_manager.hpc_health
    if hpc.get("status") != "ready":
        raise HTTPException(
            status_code=503,
            detail=f"超算环境未就绪：{hpc.get('message') or hpc.get('status')}，请先完成超算认证",
        )
    try:
        return ok(task_manager.retry_outputs(task_id), message="已加入结果下载恢复队列")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/wrf/tasks/{task_id}/render-partial", status_code=202)
def render_partial_outputs(task_id: str) -> dict[str, Any]:
    try:
        return ok(task_manager.render_partial(task_id), message="已确认忽略坏帧并加入部分渲染队列")
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="WRF 任务不存在") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/wrf/tasks/{task_id}/result")
def task_result(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if task["status"] not in {"succeeded", "partial_success"} or not task.get("result"):
        raise HTTPException(status_code=409, detail={"message": "WRF 任务尚未完成", "status": task["status"]})
    return ok(task["result"])


@app.get("/api/wrf/display")
def display(task_id: str | None = None) -> dict[str, Any]:
    task = get_task(task_id) if task_id else store.latest_success()
    if task is None:
        return ok(None, message="暂无成功的 WRF 任务")
    if task["status"] not in {"succeeded", "partial_success"} or not task.get("result"):
        raise HTTPException(status_code=409, detail={"message": "指定 WRF 任务尚未完成", "status": task["status"]})
    return ok(task["result"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False, workers=1)
