from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np


DISPLAY_VARIABLES = {
    "T2": {"label": "2 米温度", "fallback_unit": "K", "palette": "temperature"},
    "U10": {"label": "10 米 U 风", "fallback_unit": "m s-1", "palette": "diverging"},
    "V10": {"label": "10 米 V 风", "fallback_unit": "m s-1", "palette": "diverging"},
    "PSFC": {"label": "地面气压", "fallback_unit": "Pa", "palette": "pressure"},
    "PBLH": {"label": "边界层高度", "fallback_unit": "m", "palette": "height"},
    "RAINC": {"label": "积云降水", "fallback_unit": "mm", "palette": "rain"},
    "RAINNC": {"label": "非积云降水", "fallback_unit": "mm", "palette": "rain"},
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


def _time_label(dataset: Any, source: Path) -> str:
    if "Times" in dataset.variables:
        raw = dataset.variables["Times"][0]
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


def _surface(dataset: Any, name: str) -> np.ndarray:
    variable = dataset.variables[name]
    value = np.asarray(variable[:], dtype=float)
    if value.ndim == 4:
        value = value[0, 0]
    elif value.ndim == 3:
        value = value[0]
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


def _rgba(value: np.ndarray, palette_name: str) -> tuple[np.ndarray, float, float]:
    low, high = _range(value)
    normalized = np.clip((value - low) / (high - low), 0, 1)
    palette = np.asarray(PALETTES[palette_name], dtype=float)
    positions = normalized * (len(palette) - 1)
    left = np.floor(positions).astype(int)
    right = np.clip(left + 1, 0, len(palette) - 1)
    fraction = (positions - left)[..., None]
    rgb = palette[left] * (1 - fraction) + palette[right] * fraction
    alpha = np.where(np.isfinite(value), 185, 0)[..., None]
    return np.flipud(np.concatenate([rgb, alpha], axis=-1).astype(np.uint8)), low, high


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
    match = re.search(r"wrfout_(d\d{2})_(\d{4}-\d{2}-\d{2})_(\d{2}:\d{2}:\d{2})", name)
    if not match:
        return "", ""
    return match.group(1).lower(), f"{match.group(2)}T{match.group(3)}Z"


def render_run(
    task_id: str,
    raw_dir: Path,
    output_dir: Path,
    task_request: dict[str, Any],
    cycle: str,
    *,
    allow_partial: bool = False,
    excluded_outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    Dataset, Image = _runtime()
    output_dir.mkdir(parents=True, exist_ok=True)
    domain_map: dict[str, dict[str, Any]] = {}
    validation = validate_wrfout_files(raw_dir)
    invalid = list(excluded_outputs or []) + list(validation["invalid"])
    if invalid and not allow_partial:
        names = ", ".join(item.get("name", "unknown") for item in invalid[:5])
        raise RuntimeError(f"wrfout 完整性校验失败：{names}；可选择忽略坏帧并部分渲染")
    sources = validation["valid"]
    if not sources:
        raise RuntimeError("未找到可渲染的 wrfout 文件")
    for source in sources:
        with Dataset(str(source)) as dataset:
            domain = _domain(source, dataset)
            time_label = _time_label(dataset, source)
            lat, lon = _lat_lon(dataset)
            extent = [float(np.nanmin(lon)), float(np.nanmin(lat)), float(np.nanmax(lon)), float(np.nanmax(lat))]
            domain_item = domain_map.setdefault(
                domain,
                {
                    "id": domain,
                    "dx": float(getattr(dataset, "DX", 0.0) or 0.0),
                    "dy": float(getattr(dataset, "DY", 0.0) or 0.0),
                    "grid": [int(lon.shape[1]), int(lat.shape[0])],
                    "extent": extent,
                    "times": [],
                    "variables": {},
                },
            )
            if time_label and time_label not in domain_item["times"]:
                domain_item["times"].append(time_label)
            for name, definition in DISPLAY_VARIABLES.items():
                if name not in dataset.variables:
                    continue
                value = _surface(dataset, name)
                rgba, low, high = _rgba(value, definition["palette"])
                stamp = re.sub(r"[^0-9]", "", time_label)[:14] or source.name[-19:].replace(":", "").replace("_", "")
                relative = Path("runs") / task_id / "rendered" / domain / name / f"{stamp}.webp"
                target = output_dir.parents[1] / relative
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
                        "frames": [],
                    },
                )
                valid = value[np.isfinite(value)]
                item["frames"].append(
                    {
                        "time": time_label,
                        "url": "/data/WRF/" + relative.as_posix(),
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
    expected_times: list[str] = []
    cursor = start
    while cursor <= end:
        expected_times.append(cursor.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        cursor += timedelta(hours=1)
    missing_by_domain: dict[str, list[str]] = {}
    for domain in domains:
        available = set(domain["times"])
        missing = [value for value in expected_times if value not in available]
        if missing:
            missing_by_domain[domain["id"]] = missing
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
    partial = bool(excluded or missing_by_domain)
    manifest = {
        "schema_version": "1.1",
        "image_format": "webp",
        "business_type": "WRF",
        "task_id": task_id,
        "data_source": "GFS",
        "gfs_cycle": cycle,
        "start_time": task_request["start_time"],
        "end_time": task_request["end_time"],
        "center": task_request["center"],
        "default_domain": domains[-1]["id"],
        "default_variable": "T2",
        "domains": domains,
        "quality": {
            "status": "partial" if partial else "complete",
            "warnings": (["部分 wrfout 无法读取，已从可视化中排除"] if excluded else [])
            + (["部分产品时次缺失"] if missing_by_domain else []),
            "excluded_frames": excluded,
            "missing_times": missing_by_domain,
        },
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    manifest_path = output_dir / "scene.meta.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
