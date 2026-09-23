from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .schemas import FORCING_DX_M, FORCING_SCHEMA_VERSION, FORCING_TOPOLOGY_ID, valid_times


# Standard product assets only. Native OpenFOAM cases and unbounded LES time
# series stay in managed archival storage and are never browser-delivered.
SAFE_ASSET_SUFFIXES = {".webp", ".json", ".geojson", ".bin", ".png", ".nc", ".tif", ".tiff", ".vtk", ".vtu", ".vtp", ".pdf", ".csv"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() not in SAFE_ASSET_SUFFIXES:
        raise ValueError(f"产品资产路径不安全或类型不允许：{value}")
    return path


def _check_assets(root: Path, assets: list[dict[str, Any]]) -> None:
    if not assets:
        raise ValueError("产品清单缺少资产校验和")
    for asset in assets:
        relative = _safe_relative(str(asset.get("path") or ""))
        path = root / relative
        if not path.is_file():
            raise ValueError(f"产品资产不存在：{relative}")
        if int(asset.get("size_bytes") or -1) != path.stat().st_size:
            raise ValueError(f"产品资产大小不匹配：{relative}")
        if str(asset.get("sha256") or "") != sha256_file(path):
            raise ValueError(f"产品资产 SHA-256 不匹配：{relative}")


def _check_standard_products(manifest: dict[str, Any], assets: list[dict[str, Any]]) -> None:
    products = manifest.get("standard_products") or {}
    required = {"cf_netcdf", "geotiff_layers", "vtk_scene", "quality_report"}
    if not required.issubset(products):
        raise ValueError("标准产品包必须包含 CF-NetCDF、GeoTIFF、VTK 与质量报告")
    asset_paths = {str(asset.get("path")) for asset in assets}
    for key in required:
        values = products.get(key)
        paths = values if isinstance(values, list) else [values]
        if not paths or any(str(path) not in asset_paths for path in paths):
            raise ValueError(f"标准产品 {key} 未列入经过校验的资产")


def _check_browser_preview(manifest: dict[str, Any], assets: list[dict[str, Any]]) -> None:
    """Allow a small, explicit browser view without exposing CFD volumes."""
    preview = manifest.get("browser_preview")
    if preview is None:
        return
    if not isinstance(preview, dict):
        raise ValueError("browser_preview 必须是对象")
    asset_paths = {str(asset.get("path")) for asset in assets}
    scene = preview.get("scene_manifest")
    if scene and str(scene) not in asset_paths:
        raise ValueError("三维场景清单未列入经过校验的资产")
    layers = preview.get("layers") or {}
    if not isinstance(layers, dict):
        raise ValueError("browser_preview.layers 必须按 AGL 高度组织")
    for height, variables in layers.items():
        try:
            if int(height) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("浏览器预览高度必须是正 AGL 数值") from None
        if not isinstance(variables, dict):
            raise ValueError("每个浏览器预览高度必须提供变量映射")
        for variable, path in variables.items():
            candidate = _safe_relative(str(path))
            if str(candidate) not in asset_paths:
                raise ValueError(f"浏览器预览 {height} m / {variable} 未列入经过校验的资产")
            if candidate.suffix.lower() not in {".webp", ".png"}:
                raise ValueError("浏览器预览只能使用轻量 WebP 或 PNG 栅格")


def _expected_geometry(expected_request: dict[str, Any] | None) -> tuple[int | None, int | None]:
    aoi = (expected_request or {}).get("aoi") or {}
    try:
        return int(aoi["width_m"]), int(aoi["height_m"])
    except (KeyError, TypeError, ValueError):
        return None, None


def validate_product_bundle(root: Path, task_id: str, expected_request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a runner-produced v2 bundle before publication.

    A bundle is either a set of independent RANS steady snapshots or finite LES
    statistical windows. Neither may masquerade as a continuous 1 m forecast.
    """
    manifest_path = root / "downscale.product.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("缺少或无法解析 downscale.product.json") from exc
    if manifest.get("schema_version") != "downscale-product/v2":
        raise ValueError("不支持的高分产品清单版本")
    if manifest.get("workflow_id") != task_id:
        raise ValueError("产品清单任务 ID 不匹配")
    kind = manifest.get("product_kind")
    if kind not in {"wrf_to_cfd_rans_snapshot_set", "wrf_to_cfd_les_episode_set"}:
        raise ValueError("产品类型必须是 RANS 快照集或 LES 统计时段集")
    forcing = manifest.get("mesoscale_forcing") or {}
    forcing_times = forcing.get("valid_times_utc") or []
    expected_times = None
    if expected_request and expected_request.get("start_time"):
        start = datetime.fromisoformat(str(expected_request["start_time"]).replace("Z", "+00:00"))
        end_raw = expected_request.get("end_time")
        end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")) if end_raw else None
        expected_times = valid_times(start, end)
    if expected_times is not None and forcing_times != expected_times:
        raise ValueError("产品引用的 forcing 时次与任务起止时间不一致")
    if not forcing_times:
        raise ValueError("产品必须引用逐小时 forcing 时次")
    request_wrf = (expected_request or {}).get("wrf") or {}
    request_domains = request_wrf.get("domains") or []
    request_final_dx = float((request_domains[-1] or {}).get("dx_m") or 0) if request_domains else 0.0
    new_forcing = (expected_request or {}).get("upstream_mode") == "reuse_10m" or (
        (expected_request or {}).get("upstream_mode") == "full_chain" and abs(request_final_dx - FORCING_DX_M) <= 0.02
    )
    expected_spacing = FORCING_DX_M if new_forcing else 34.293556
    if forcing.get("horizontal_spacing_m") != expected_spacing:
        label = "约 10 m d08" if new_forcing else "历史 34.293556 m"
        raise ValueError(f"产品必须引用{label} forcing")
    if new_forcing and forcing.get("schema_version") != FORCING_SCHEMA_VERSION:
        raise ValueError("CFD 产品必须引用 mesoscale-forcing/v2 manifest")
    if new_forcing and forcing.get("topology_id") != FORCING_TOPOLOGY_ID:
        raise ValueError("CFD 产品必须引用固定 d01-d08 约 10 m forcing 拓扑")
    if new_forcing and expected_request and expected_request.get("execution_scope") == "wrf_cfd":
        expected_profile = expected_request.get("parameterization_profile_id") or "standard-rans-thermal/v1"
        actual_profile = (manifest.get("parameterization") or {}).get("id")
        if actual_profile != expected_profile:
            raise ValueError("产品使用的高分参数化方案与任务合同不一致")
    expected_surface_mode = (expected_request or {}).get("surface_mode")
    if expected_surface_mode:
        surface = manifest.get("surface_model") or {}
        if surface.get("mode") != expected_surface_mode:
            raise ValueError("产品地表模式与任务合同不一致")
        if expected_surface_mode == "auto_flat":
            snapshots = surface.get("snapshots") or {}
            if not snapshots or any(
                value.get("method") != "wrf_lu_index_znt_median/v1" or float(value.get("roughness_length_m") or 0) <= 0
                for value in snapshots.values()
            ):
                raise ValueError("平坦地表产品缺少逐快照 WRF 粗糙度证据")
    assets = manifest.get("assets") or []
    _check_assets(root, assets)
    _check_standard_products(manifest, assets)
    _check_browser_preview(manifest, assets)

    expected_width, expected_height = _expected_geometry(expected_request)
    product = manifest.get("publication_product") or {}
    if product.get("horizontal_sampling_m") != 1:
        raise ValueError("高分产品必须以 1 m 水平采样发布")
    if expected_width is not None and (product.get("width_m") != expected_width or product.get("height_m") != expected_height):
        raise ValueError("产品发布 AOI 与受管任务不一致")

    temporal = manifest.get("temporal_semantics") or {}
    if temporal.get("continuous_1m_time_series") is not False or temporal.get("temporal_interpolation") != "forbidden":
        raise ValueError("高分产品不得声明连续 1 m 时间序列或时间插值")
    if kind == "wrf_to_cfd_rans_snapshot_set":
        snapshots = manifest.get("rans_snapshots") or []
        if not 1 <= len(snapshots) <= 6 or temporal.get("kind") != "independent_representative_steady_snapshots":
            raise ValueError("RANS 产品需要 1–6 个独立代表稳态快照")
        for snapshot in snapshots:
            forcing_ref = snapshot.get("forcing") or {}
            solver = snapshot.get("solver") or {}
            if forcing_ref.get("temporal_match") != "exact" or solver.get("solution_type") != "steady_rans":
                raise ValueError("每个 RANS 快照必须与 forcing 精确匹配并声明稳态解")
    else:
        episodes = manifest.get("les_episodes") or []
        if not 1 <= len(episodes) <= 2 or temporal.get("kind") != "finite_les_time_window_statistics":
            raise ValueError("LES 产品需要 1–2 个有限统计时段")
        for episode in episodes:
            forcing_ref = episode.get("forcing") or {}
            solver = episode.get("solver") or {}
            window = episode.get("statistics_window") or {}
            if forcing_ref.get("temporal_match") != "exact" or solver.get("solution_type") != "les_time_window_statistics":
                raise ValueError("每个 LES 时段必须精确绑定 forcing 并声明统计解")
            if not window.get("start_utc") or not window.get("end_utc"):
                raise ValueError("LES 产品必须记录统计时间窗口")
    return manifest


def atomically_publish_bundle(source: Path, destination_root: Path, task_id: str, expected_request: dict[str, Any] | None = None) -> Path:
    """Copy a validated bundle to a task-private staging directory then promote."""
    destination_root.mkdir(parents=True, exist_ok=True)
    target = destination_root / task_id
    if target.exists():
        raise ValueError("该任务已有发布目录，拒绝覆盖历史产品")
    staging = destination_root / f".{task_id}.{uuid.uuid4().hex}.staging"
    try:
        shutil.copytree(source, staging, copy_function=shutil.copy2)
        validate_product_bundle(staging, task_id, expected_request)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
