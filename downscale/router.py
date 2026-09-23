from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from .schemas import (
    CertificationRequest,
    CertificationReview,
    DownscaleTaskCreate,
    DownscaleValidationRequest,
    TerrainAsset,
    TerrainAssetReview,
    TerrainAssetResolveRequest,
    TerrainAssetSubmission,
)
from .service import DownscaleService


def build_router(service: DownscaleService, viewer: Callable[[Request], tuple[str, int]], normal_tasks: Callable[[Request, int], list[dict[str, Any]]]) -> APIRouter:
    router = APIRouter(prefix="/api/wrf", tags=["high-resolution-downscale"])

    def identity(request: Request) -> tuple[str, int]:
        owner, role = viewer(request)
        if not owner:
            raise HTTPException(status_code=401, detail="Token 缺少用户身份")
        return owner, role

    def visible(task_id: str, request: Request) -> dict[str, Any]:
        owner, role = identity(request)
        try:
            return service.get_visible(task_id, owner, role)
        except KeyError:
            raise HTTPException(status_code=404, detail="高分辨率任务不存在") from None

    def admin(request: Request) -> tuple[str, int]:
        owner, role = identity(request)
        if role < 3:
            raise HTTPException(status_code=403, detail="该操作需要高分配置审核权限")
        return owner, role

    @router.get("/downscale/templates")
    def templates(request: Request) -> dict[str, Any]:
        identity(request)
        return {"code": 0, "data": service.templates(), "message": "success"}

    @router.get("/downscale/terrain-assets")
    def terrain_assets(request: Request) -> dict[str, Any]:
        identity(request)
        return {"code": 0, "data": {"items": service.terrain_assets()}, "message": "success"}

    @router.post("/downscale/terrain-assets/resolve")
    def resolve_terrain_assets(payload: TerrainAssetResolveRequest, request: Request) -> dict[str, Any]:
        identity(request)
        try:
            return {"code": 0, "data": service.resolve_terrain_assets(payload.aoi), "message": "地形资产覆盖已评估"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/downscale/terrain-assets", status_code=201)
    def register_terrain_asset(payload: TerrainAsset, request: Request) -> dict[str, Any]:
        admin(request)
        try:
            return {"code": 0, "data": service.register_terrain_asset(payload), "message": "审核地形资产已登记"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/downscale/terrain-assets/submissions", status_code=202)
    def submit_terrain_asset(payload: TerrainAssetSubmission, request: Request) -> dict[str, Any]:
        owner, _ = identity(request)
        try:
            return {"code": 0, "data": service.submit_terrain_asset(payload, owner), "message": "地形资产已提交审核"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/downscale/terrain-assets/submissions/{submission_id}/review")
    def review_terrain_asset(submission_id: str, payload: TerrainAssetReview, request: Request) -> dict[str, Any]:
        reviewer, _ = admin(request)
        try:
            return {"code": 0, "data": service.review_terrain_asset(submission_id, payload, reviewer), "message": "地形资产审核已完成"}
        except KeyError:
            raise HTTPException(status_code=404, detail="地形资产申请不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/downscale/validate")
    def validate(payload: DownscaleValidationRequest, request: Request) -> dict[str, Any]:
        owner, role = identity(request)
        if payload.upstream_mode == "reuse_34m":
            raise HTTPException(status_code=409, detail="reuse_34m 仅保留历史任务查看，不能用于新的生产 CFD 输入")
        try:
            return {"code": 0, "data": service.validate(payload, owner, role), "message": "高分辨率任务预检通过"}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/downscale/tasks", status_code=202)
    def create(payload: DownscaleTaskCreate, request: Request) -> dict[str, Any]:
        owner, role = identity(request)
        key = request.headers.get("Idempotency-Key")
        if key and (len(key) > 128 or not all(char.isalnum() or char in "-_" for char in key)):
            raise HTTPException(status_code=422, detail="幂等键格式无效")
        try:
            return {"code": 0, "data": service.create(payload, owner, key, role), "message": "高分辨率任务已进入受管队列"}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/downscale/tasks")
    def tasks(request: Request, limit: int = 50) -> dict[str, Any]:
        owner, role = identity(request)
        return {"code": 0, "data": {"items": service.list_visible(limit, owner, role)}, "message": "success"}

    @router.get("/downscale/tasks/{task_id}")
    def detail(task_id: str, request: Request) -> dict[str, Any]:
        return {"code": 0, "data": visible(task_id, request), "message": "success"}

    @router.get("/downscale/tasks/{task_id}/result")
    def result(task_id: str, request: Request) -> dict[str, Any]:
        task = visible(task_id, request)
        if task.get("quality", {}).get("status") != "complete" or task.get("publication", {}).get("status") not in {"published", "success", "internal_preview"}:
            raise HTTPException(status_code=409, detail="高分辨率产品尚未通过质量门")
        return {"code": 0, "data": {**(task.get("result") or {}), "publication": task.get("publication"), "certification": task.get("certification")}, "message": "success"}

    @router.get("/downscale/tasks/{task_id}/logs")
    def logs(task_id: str, request: Request, after: int = 0, limit: int = 65536) -> dict[str, Any]:
        owner, role = identity(request)
        try:
            return {"code": 0, "data": service.logs(task_id, owner, role, after, limit), "message": "success"}
        except KeyError:
            raise HTTPException(status_code=404, detail="高分辨率任务不存在") from None

    @router.post("/downscale/tasks/{task_id}/cancel")
    def cancel(task_id: str, request: Request) -> dict[str, Any]:
        owner, role = identity(request)
        try:
            return {"code": 0, "data": service.cancel(task_id, owner, role), "message": "取消请求已处理"}
        except KeyError:
            raise HTTPException(status_code=404, detail="高分辨率任务不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/downscale/tasks/{task_id}/retry", status_code=202)
    def retry(task_id: str, request: Request) -> dict[str, Any]:
        owner, role = identity(request)
        try:
            return {"code": 0, "data": service.retry(task_id, owner, role), "message": "已开始新的高分辨率尝试"}
        except KeyError:
            raise HTTPException(status_code=404, detail="高分辨率任务不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/downscale/tasks/{task_id}/certification-requests", status_code=202)
    def request_certification(task_id: str, payload: CertificationRequest, request: Request) -> dict[str, Any]:
        owner, role = identity(request)
        try:
            return {"code": 0, "data": service.request_certification(task_id, owner, role, payload), "message": "配置认证申请已提交"}
        except KeyError:
            raise HTTPException(status_code=404, detail="高分辨率任务不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/downscale/certification-requests/{request_id}/review")
    def review_certification(request_id: str, payload: CertificationReview, request: Request) -> dict[str, Any]:
        reviewer, _ = admin(request)
        try:
            return {"code": 0, "data": service.review_certification(request_id, reviewer, payload), "message": "配置认证审核已完成"}
        except KeyError:
            raise HTTPException(status_code=404, detail="认证申请不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/workbench/tasks")
    def workbench_tasks(request: Request, limit: int = 50) -> dict[str, Any]:
        owner, role = identity(request)
        normal = normal_tasks(request, limit)
        highres = service.list_visible(limit, owner, role)
        items = sorted([*normal, *highres], key=lambda item: str(item.get("created_at") or ""), reverse=True)[:max(1, min(200, limit))]
        return {"code": 0, "data": {"items": items}, "message": "success"}

    return router
