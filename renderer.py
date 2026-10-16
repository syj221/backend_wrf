from __future__ import annotations

import json
import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.transform import array_bounds, from_origin
from rasterio.warp import calculate_default_transform, reproject


DISPLAY_VARIABLES = {
    "T2": {"label": "2 米温度", "fallback_unit": "K", "palette": "temperature"},
    "U10": {"label": "10 米 U 风", "fallback_unit": "m s-1", "palette": "diverging"},
    "V10": {"label": "10 米 V 风", "fallback_unit": "m s-1", "palette": "diverging"},
    "PSFC": {"label": "地面气压", "fallback_unit": "Pa", "palette": "pressure"},
    "PBLH": {"label": "边界层高度", "fallback_unit": "m", "palette": "height"},
    "RAINC": {"label": "积云累计降水", "fallback_unit": "mm", "palette": "rain"},
    "RAINNC": {"label": "网格尺度累计降水", "fallback_unit": "mm", "palette": "rain"},
}

PALETTES = {
    "temperature": [(30, 64, 175), (71, 148, 255), (238, 238, 238), (255, 170, 70), (180, 4, 38)],
    "diverging": [(30, 64, 175), (125, 180, 255), (245, 245, 245), (255, 150, 100), (180, 4, 38)],
    "pressure": [(49, 46, 129), (37, 99, 235), (34, 197, 94), (250, 204, 21), (220, 38, 38)],
    "height": [(15, 23, 42), (37, 99, 235), (34, 197, 94), (250, 204, 21), (249, 115, 22)],
    "rain": [(248, 250, 252), (191, 219, 254), (56, 189, 248), (37, 99, 235), (30, 58, 138)],
}


def _runtime():
    try:
        from netCDF4 import Dataset
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("渲染 WRF 结果需要 netCDF4 与 Pillow") from exc
    return Dataset, Image


def _time_label(dataset: Any, source: Path, index: int = 0) -> str:
    if "Times" in dataset.variables:
        raw = dataset.variables["Times"][index]
        try:
            return b"".join(raw).decode("ascii", errors="ignore").replace("_", "T") + "Z"
        except TypeError:
            return "".join(str(item) for item in raw).replace("_", "T") + "Z"
    match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2}:\d{2}:\d{2})", source.name)
    return f"{match.group(1)}T{match.group(2)}Z" if match else ""


def _domain(source: Path, dataset: Any) -> str:
    match = re.search(r"wrfout_(d\d{2})", source.name, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    grid_id = getattr(dataset, "GRID_ID", 1)
    return f"d{int(grid_id):02d}"


def _lat_lon(dataset: Any) -> tuple[np.ndarray, np.ndarray]:
    lat = np.asarray(dataset.variables["XLAT"][0], dtype=float)
    lon = np.asarray(dataset.variables["XLONG"][0], dtype=float)
    return lat, lon


def _surface(dataset: Any, name: str, index: int = 0) -> np.ndarray:
    variable = dataset.variables[name]
    value = np.asarray(variable[:], dtype=float)
    if value.ndim == 4:
        value = value[index, 0]
    elif value.ndim == 3:
        value = value[index]
    if value.ndim != 2:
        raise ValueError(f"变量 {name} 不是二维地面场")
    return value


def _range(value: np.ndarray) -> tuple[float, float]:
    valid = value[np.isfinite(value)]
    if not valid.size:
        return 0.0, 1.0
    low, high = np.nanpercentile(valid, [2, 98])
    if not np.isfinite(low) or not np.isfinite(high) or low == high:
        low, high = float(np.nanmin(valid)), float(np.nanmax(valid))
    if low == high:
        high = low + 1.0
    return float(low), float(high)


def _rgba(value: np.ndarray, palette_name: str, low: float | None = None,
          high: float | None = None) -> tuple[np.ndarray, float, float]:
    if low is None or high is None:
        low, high = _range(value)
    normalized = np.where(np.isfinite(value), np.clip((value - low) / (high - low), 0, 1), 0)
    palette = np.asarray(PALETTES[palette_name], dtype=float)
    positions = normalized * (len(palette) - 1)
    left = np.floor(positions).astype(int)
    right = np.clip(left + 1, 0, len(palette) - 1)
    fraction = (positions - left)[..., None]
    rgb = palette[left] * (1 - fraction) + palette[right] * fraction
    alpha = np.where(np.isfinite(value), 185, 0)[..., None]
    return np.flipud(np.concatenate([rgb, alpha], axis=-1).astype(np.uint8)), low, high


def _footprint(lat: np.ndarray, lon: np.ndarray) -> dict[str, Any]:
    top = [[float(lon[-1, j]), float(lat[-1, j])] for j in range(lon.shape[1])]
    right = [[float(lon[i, -1]), float(lat[i, -1])] for i in range(lon.shape[0] - 2, -1, -1)]
    bottom = [[float(lon[0, j]), float(lat[0, j])] for j in range(lon.shape[1] - 2, -1, -1)]
    left = [[float(lon[i, 0]), float(lat[i, 0])] for i in range(1, lon.shape[0] - 1)]
    ring = top + right + bottom + left
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def _reproject_rgba(rgba: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                    dataset: Any) -> tuple[np.ndarray, list[float], float]:
    """Map cell-centred WRF Lambert grid to a north-up Web Mercator raster."""
    projection = int(getattr(dataset, "MAP_PROJ", 0))
    if projection not in {1, 2, 3}:
        raise ValueError("当前普通 WRF 产品缺少受支持的 Lambert/Polar/Mercator 投影元数据")
    truelat1 = float(getattr(dataset, "TRUELAT1"))
    truelat2 = float(getattr(dataset, "TRUELAT2", truelat1))
    stand_lon = float(getattr(dataset, "STAND_LON"))
    projection_definition = {1: f"+proj=lcc +lat_1={truelat1} +lat_2={truelat2} +lat_0={truelat1}",
        2: f"+proj=stere +lat_0={90 if truelat1 >= 0 else -90} +lat_ts={truelat1}",
        3: f"+proj=merc +lat_ts={truelat1}"}[projection]
    src_crs = CRS.from_proj4(f"{projection_definition} +lon_0={stand_lon} +R=6370000 +units=m +no_defs")
    forward = Transformer.from_crs("EPSG:4326", src_crs, always_xy=True)
    x00, y00 = forward.transform(float(lon[0, 0]), float(lat[0, 0]))
    x_end, _ = forward.transform(float(lon[0, -1]), float(lat[0, -1]))
    _, y_end = forward.transform(float(lon[-1, 0]), float(lat[-1, 0]))
    height, width = lat.shape
    if width < 2 or height < 2:
        raise ValueError("WRF 地理网格至少需要 2×2 个格点")
    dx = (x_end - x00) / (width - 1)
    dy = (y_end - y00) / (height - 1)
    if dx <= 0 or dy <= 0:
        raise ValueError("WRF 地理网格方向或分辨率无效")
    expected_dx = float(getattr(dataset, "DX", dx))
    expected_dy = float(getattr(dataset, "DY", dy))
    if abs(dx - expected_dx) > max(1.0, expected_dx * 0.05) or abs(dy - expected_dy) > max(1.0, expected_dy * 0.05):
        raise ValueError("WRF 经纬度网格与 DX/DY 投影尺度不一致")
    x_corner, y_corner = forward.transform(float(lon[-1, -1]), float(lat[-1, -1]))
    error = max(abs(x_corner - x_end), abs(y_corner - y_end))
    if error > max(dx, dy) * 0.1:
        raise ValueError("WRF 投影控制点偏差过大，禁止生成定位不可信的产品")
    src_transform = from_origin(x00 - dx / 2, y_end + dy / 2, dx, dy)
    src_bounds = array_bounds(height, width, src_transform)
    dst_crs = CRS.from_epsg(3857)
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs.to_wkt(), dst_crs.to_wkt(), width, height, *src_bounds,
    )
    if dst_width < 1 or dst_height < 1 or dst_width * dst_height > 8 * width * height:
        raise ValueError("WRF 投影重采样尺寸异常")
    source = np.moveaxis(rgba, -1, 0)
    destination = np.zeros((4, dst_height, dst_width), dtype=np.uint8)
    for band in range(4):
        reproject(source[band], destination[band],
                  src_transform=src_transform, src_crs=src_crs.to_wkt(),
                  dst_transform=dst_transform, dst_crs=dst_crs.to_wkt(),
                  resampling=Resampling.nearest if band == 3 else Resampling.bilinear)
    west, south, east, north = array_bounds(dst_height, dst_width, dst_transform)
    inverse = Transformer.from_crs(dst_crs, "EPSG:4326", always_xy=True)
    geo_west, geo_south = inverse.transform(west, south)
    geo_east, geo_north = inverse.transform(east, north)
    return np.moveaxis(destination, 0, -1), [geo_west, geo_south, geo_east, geo_north], error


def validate_wrfout_files(raw_dir: Path) -> dict[str, Any]:
    Dataset, _Image = _runtime()
    sources = sorted(path for path in raw_dir.glob("wrfout_d*_*") if path.is_file())
    valid: list[Path] = []
    invalid: list[dict[str, Any]] = []
    for source in sources:
        try:
            with Dataset(str(source)) as dataset:
                missing = [name for name in ("XLAT", "XLONG", "Times") if name not in dataset.variables]
                if missing:
                    raise RuntimeError("缺少变量 " + ", ".join(missing))
                _lat_lon(dataset)
            valid.append(source)
        except Exception as exc:
            invalid.append({"name": source.name, "reason": str(exc)[:300]})
    return {"valid": valid, "invalid": invalid, "complete": bool(valid) and not invalid}


def _filename_time(name: str) -> tuple[str, str]:
    match = re.search(r"wrfout_(d\d{2})_(\d{4}-\d{2}-\d{2})_(\d{2}[:-]\d{2}[:-]\d{2})", name)
    if not match:
        return "", ""
    time_text = match.group(3).replace("-", ":")
    return match.group(1).lower(), f"{match.group(2)}T{time_text}Z"


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _output_time_tolerance_seconds(
    task_request: dict[str, Any],
    domains: list[dict[str, Any]],
) -> int:
    request_domains = list(task_request.get("domains") or [])
    dx = (request_domains[0].get("dx") if request_domains else None) or (
        domains[0].get("dx") if domains else 0
    )
    try:
        # The outer-domain step bounds output-time alignment tolerance.
        configured = task_request.get("time_step_seconds") or (task_request.get("namelist") or {}).get("input",{}).get("domains",{}).get("time_step")
        if type(configured) is int and configured > 0:
            return configured
        return max(1, 5 * max(1, int(float(dx) / 1000)))
    except (TypeError, ValueError):
        return 1


def _align_output_times(
    domains: list[dict[str, Any]],
    expected_times: list[str],
    tolerance_seconds: int,
) -> list[dict[str, Any]]:
    expected = [(label, _parse_utc(label)) for label in expected_times]
    expected = [(label, value) for label, value in expected if value is not None]
    adjustments: list[dict[str, Any]] = []
    for domain in domains:
        replacements: dict[str, str] = {}
        offsets: dict[str, int] = {}
        claimed: set[str] = set()
        candidates: list[tuple[int, str, str, int]] = []
        for source_time in sorted(set(domain.get("times") or [])):
            source_value = _parse_utc(source_time)
            if source_value is None or not expected:
                continue
            target_time, target_value = min(
                expected,
                key=lambda item: abs((source_value - item[1]).total_seconds()),
            )
            offset = int((source_value - target_value).total_seconds())
            candidates.append((abs(offset), source_time, target_time, offset))
        for absolute_offset, source_time, target_time, offset in sorted(candidates):
            if absolute_offset > tolerance_seconds or target_time in claimed:
                continue
            claimed.add(target_time)
            replacements[source_time] = target_time
            offsets[source_time] = offset
            if source_time != target_time:
                adjustments.append({
                    "domain": domain["id"],
                    "source_time": source_time,
                    "time": target_time,
                    "offset_seconds": offset,
                })
        domain["times"] = sorted({replacements.get(value, value) for value in domain.get("times") or []})
        for variable in domain.get("variables") or []:
            for frame in variable.get("frames") or []:
                source_time = str(frame.get("time") or "")
                target_time = replacements.get(source_time, source_time)
                if target_time != source_time:
                    frame["source_time"] = source_time
                    frame["time_offset_seconds"] = offsets[source_time]
                    frame["time"] = target_time
            variable["frames"].sort(key=lambda frame: frame["time"])
    return adjustments


def _source_metadata(data_source: Any, cycle: str) -> dict[str, str]:
    """Keep forcing provenance source-specific in the published manifest."""
    source = str(data_source or "gfs").strip().lower()
    if source in {"ec", "ecmwf"}:
        return {"data_source": "ECMWF", "ecmwf_cycle": cycle}
    return {"data_source": "GFS", "gfs_cycle": cycle}


def render_run(
    task_id: str,
    raw_dir: Path,
    output_dir: Path,
    task_request: dict[str, Any],
    cycle: str,
    *,
    allow_partial: bool = False,
    excluded_outputs: list[dict[str, Any]] | None = None,
    asset_prefix: str | None = None,
) -> dict[str, Any]:
    Dataset, Image = _runtime()
    output_dir.mkdir(parents=True, exist_ok=True)
    domain_map: dict[str, dict[str, Any]] = {}
    domain_config = {d["id"]:d for d in task_request.get("domains") or []}
    validation = validate_wrfout_files(raw_dir)
    invalid = list(excluded_outputs or []) + list(validation["invalid"])
    if invalid and not allow_partial:
        names = ", ".join(item.get("name", "unknown") for item in invalid[:5])
        raise RuntimeError(f"wrfout 完整性校验失败：{names}；可选择忽略不可读帧并部分渲染")
    sources = validation["valid"]
    if not sources:
        raise RuntimeError("未找到可渲染的 wrfout 文件")
    # Keep one scale per domain/variable so animation colours remain comparable.
    display_ranges: dict[tuple[str, str], tuple[float, float]] = {}
    for source in sources:
        with Dataset(str(source)) as dataset:
            domain = _domain(source, dataset)
            for name in DISPLAY_VARIABLES:
                if name not in dataset.variables:
                    continue
                for frame_index in range(len(dataset.variables["Times"]) if "Times" in dataset.variables else 1):
                    low, high = _range(_surface(dataset, name, frame_index))
                    previous = display_ranges.get((domain, name))
                    display_ranges[(domain, name)] = (
                        min(low, previous[0]) if previous else low,
                        max(high, previous[1]) if previous else high,
                    )
    for source in sources:
        with Dataset(str(source)) as dataset:
            domain = _domain(source, dataset)
            for frame_index in range(len(dataset.variables["Times"]) if "Times" in dataset.variables else 1):
                time_label = _time_label(dataset, source, frame_index)
                lat, lon = _lat_lon(dataset)
                extent = [float(np.nanmin(lon)), float(np.nanmin(lat)), float(np.nanmax(lon)), float(np.nanmax(lat))]
                domain_item = domain_map.setdefault(
                    domain,
                    {
                        "id": domain,
                        "parent_id":domain_config.get(domain,{}).get("parent_id",0 if domain == "d01" else None),
                        "dx": float(getattr(dataset, "DX", 0.0) or 0.0),
                        "dy": float(getattr(dataset, "DY", 0.0) or 0.0),
                        "grid": [int(lon.shape[1]), int(lat.shape[0])],
                        "extent": extent,
                        "footprint": _footprint(lat, lon),
                        "geolocation": {"status": "reprojected", "source": "XLAT/XLONG + WRF projection metadata", "target_crs": "EPSG:3857"},
                        "times": [],
                        "variables": {},
                    },
                )
                if time_label and time_label not in domain_item["times"]:
                    domain_item["times"].append(time_label)
                for name, definition in DISPLAY_VARIABLES.items():
                    if name not in dataset.variables:
                        continue
                    value = _surface(dataset, name, frame_index)
                    if task_request.get("recovery_policy") == "confirm" and not np.isfinite(value).all():
                        invalid.append({"name": source.name, "reason": f"non_finite_values:{name}"})
                        continue
                    stable_low, stable_high = display_ranges[(domain, name)]
                    rgba, low, high = _rgba(value, definition["palette"], stable_low, stable_high)
                    rgba, image_extent, control_error = _reproject_rgba(rgba, lat, lon, dataset)
                    domain_item["geolocation"]["max_control_error_m"] = max(
                        float(domain_item["geolocation"].get("max_control_error_m") or 0), control_error,
                    )
                    domain_item["extent"] = image_extent
                    stamp = re.sub(r"[^0-9]", "", time_label)[:14] or source.name[-19:].replace(":", "").replace("_", "")
                    relative = Path("runs") / task_id / "rendered" / domain / name / f"{stamp}.webp"
                    target = output_dir / "rendered" / domain / name / f"{stamp}.webp"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(rgba).save(target, "WEBP", quality=88, method=6)
                    variable = dataset.variables[name]
                    item = domain_item["variables"].setdefault(
                        name,
                        {
                            "name": name,
                            "label": definition["label"],
                            "units": str(getattr(variable, "units", definition["fallback_unit"])),
                            "description": str(getattr(variable, "description", definition["label"])),
                            "palette": [list(color) for color in PALETTES[definition["palette"]]],
                            "display_range": [low, high],
                            "frames": [],
                        },
                    )
                    valid = value[np.isfinite(value)]
                    item["frames"].append(
                        {
                            "time": time_label,
                            "url": (asset_prefix.rstrip("/") + f"/rendered/{domain}/{name}/{stamp}.webp") if asset_prefix else "/data/WRF/" + relative.as_posix(),
                            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                            "min": float(np.nanmin(valid)) if valid.size else None,
                            "max": float(np.nanmax(valid)) if valid.size else None,
                            "mean": float(np.nanmean(valid)) if valid.size else None,
                            "display_min": low,
                            "display_max": high,
                        }
                    )
    domains = []
    for domain in sorted(domain_map):
        item = domain_map[domain]
        item["times"].sort()
        item["variables"] = list(item["variables"].values())
        for variable in item["variables"]:
            variable["frames"].sort(key=lambda frame: frame["time"])
        domains.append(item)
    start = datetime.fromisoformat(str(task_request["start_time"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(task_request["end_time"]).replace("Z", "+00:00"))
    from namelist_config import output_settings
    from schemas import configured_spinup_hours
    expected_by_domain = {}
    model_start = start - timedelta(hours=configured_spinup_hours(task_request))
    for index, config_domain in enumerate(task_request.get("domains") or []):
        seconds, begin, end_offset = output_settings(task_request, index)
        if seconds <= 0:
            raise ValueError("输出间隔必须大于0")
        cursor = model_start + timedelta(seconds=begin)
        if cursor < start:
            cursor += timedelta(seconds=seconds * math.ceil((start-cursor).total_seconds()/seconds))
        expected = []
        while cursor <= min(end, model_start + timedelta(seconds=end_offset)):
            expected.append(cursor.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
            cursor += timedelta(seconds=seconds)
        expected_by_domain[config_domain["id"]] = expected
    time_tolerance_seconds = _output_time_tolerance_seconds(task_request, domains)
    time_adjustments = []
    for domain in domains:
        time_adjustments.extend(_align_output_times([domain], expected_by_domain.get(domain["id"], []), time_tolerance_seconds))
    missing_by_domain = {}
    for domain_id, expected in expected_by_domain.items():
        present = next((d for d in domains if d["id"] == domain_id), None)
        missing = [value for value in expected if not present or value not in present["times"]]
        if missing:
            missing_by_domain[domain_id] = missing
    excluded = []
    seen_excluded: set[str] = set()
    for item in invalid:
        name = str(item.get("name") or "")
        if not name or name in seen_excluded:
            continue
        seen_excluded.add(name)
        domain, time_label = _filename_time(name)
        excluded.append(
            {
                "name": name,
                "domain": domain or None,
                "time": time_label or None,
                "reason": str(item.get("reason") or "invalid_output"),
            }
        )
    missing_variables = {}
    if task_request.get("recovery_policy") == "confirm":
        for domain in domains:
            for name in ("T2", "U10", "V10", "PSFC"):
                variable = next((v for v in domain["variables"] if v["name"] == name), None)
                times = {frame["time"] for frame in variable["frames"]} if variable else set()
                missing = [value for value in expected_by_domain.get(domain["id"], []) if value not in times]
                if missing:
                    missing_variables[f"{domain['id']}.{name}"] = missing
    partial = bool(excluded or missing_by_domain or missing_variables)
    warnings = (
        (["部分 wrfout 无法读取，已从可视化中排除"] if excluded else [])
        + (["部分产品时次缺失"] if missing_by_domain else [])
        + (["关键变量或变量时次缺失"] if missing_variables else [])
    )
    if time_adjustments:
        warnings.append(f"{len(time_adjustments)} 个输出时次已在容差内对齐至计划时次")
    default_domain = next((d for d in domains if d["id"] == "d01"),domains[0])
    default_variables = [
        str(item.get("name") or "")
        for item in (default_domain.get("variables") or [])
        if item.get("name")
    ] if domains else []
    default_variable = "T2" if "T2" in default_variables else (default_variables[0] if default_variables else "")
    source_metadata = _source_metadata(task_request.get("data_source"), cycle)
    manifest = {
        "schema_version": "1.3",
        "image_format": "webp",
        "business_type": "WRF",
        "task_id": task_id,
        "forcing_cycle": cycle,
        **source_metadata,
        "start_time": task_request["start_time"],
        "end_time": task_request["end_time"],
        "history_interval_minutes": int(task_request.get("history_interval_minutes") or 60),
        "output_intervals_seconds": {d["id"]: output_settings(task_request, i)[0] for i, d in enumerate(task_request.get("domains") or [])},
        "center": task_request["center"],
        "default_domain": default_domain["id"],
        "default_variable": default_variable,
        "domains": domains,
        "quality": {
            "status": "partial" if partial else "complete",
            "warnings": warnings,
            "excluded_frames": excluded,
            "unreadable_frames": excluded,
            "missing_times": missing_by_domain,
            "missing_variables": missing_variables,
            "time_tolerance_seconds": time_tolerance_seconds,
            "time_adjustments": time_adjustments,
            "geolocation": "reprojected",
            "limitations": [
                "WebP 已从 WRF 网格重投影至 EPSG:3857；统计值来自原始网格，图像为展示重采样。",
                "RAINC/RAINNC 是运行累计降水（mm），不能直接解释为单个输出间隔降水量或降水强度。",
            ],
        },
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    manifest_path = output_dir / "scene.meta.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
