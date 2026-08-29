from __future__ import annotations

import hashlib
import json
import shutil
import sys
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select, update


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from DB.migrate import init_database
from DB.schema import public_info, users, wrf_info


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _rewrite_manifest_urls(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    result = deepcopy(manifest)
    old_prefix = f"/data/WRF/runs/{task_id}/"
    new_prefix = f"/data/WRF/workbench/{task_id}/"
    for domain in result.get("domains") or []:
        for variable in domain.get("variables") or []:
            for frame in variable.get("frames") or []:
                url = str(frame.get("url") or "")
                if url.startswith(old_prefix):
                    frame["url"] = new_prefix + url.removeprefix(old_prefix)
    result["source_origin"] = "workbench"
    result["source_task_id"] = task_id
    result["catalog_meta_url"] = new_prefix + "scene.meta.json"
    return result


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _asset_rows(file_uuid: str, task_id: str, manifest: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reference_time = _parse_time(manifest.get("start_time"))
    for domain in manifest.get("domains") or []:
        domain_id = str(domain.get("id") or "")
        grid = domain.get("grid") or []
        extent = domain.get("extent") or []
        for variable in domain.get("variables") or []:
            name = str(variable.get("name") or "")
            for frame_index, frame in enumerate(variable.get("frames") or []):
                url = str(frame.get("url") or "")
                valid_time = _parse_time(frame.get("time"))
                forecast_hour = None
                if reference_time and valid_time:
                    forecast_hour = int(round((valid_time - reference_time).total_seconds() / 3600))
                identity = f"{task_id}|{domain_id}|{name}|{frame.get('time')}|{url}"
                rows.append({
                    "asset_uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                    "file_uuid": file_uuid,
                    "dataset_id": task_id,
                    "element_key": name,
                    "raw_element_name": name,
                    "element_label": variable.get("label") or variable.get("description") or name,
                    "element_kind": "forecast",
                    "raw_unit": variable.get("units"),
                    "display_unit": variable.get("units"),
                    "level_type": "surface",
                    "valid_time": valid_time,
                    "frame_index": frame_index,
                    "resolution_key": domain_id or "native",
                    "grid_width": int(grid[0]) if len(grid) >= 2 else None,
                    "grid_height": int(grid[1]) if len(grid) >= 2 else None,
                    "bbox_west": float(extent[0]) if len(extent) >= 4 else None,
                    "bbox_south": float(extent[1]) if len(extent) >= 4 else None,
                    "bbox_east": float(extent[2]) if len(extent) >= 4 else None,
                    "bbox_north": float(extent[3]) if len(extent) >= 4 else None,
                    "parsed_data_path": url.removeprefix("/data/"),
                    "webp_url": url,
                    "min_value": frame.get("min"),
                    "max_value": frame.get("max"),
                    "mean_value": frame.get("mean"),
                    "missing_ratio": None,
                    "is_default": name == manifest.get("default_variable") and frame_index == 0,
                    "asset_status": "ready",
                    "extra_json": json.dumps({"source_origin": "workbench", "source_task_id": task_id}, ensure_ascii=False),
                    "domain": domain_id,
                    "forecast_reference_time": reference_time,
                    "forecast_hour": forecast_hour,
                    "dx_m": domain.get("dx"),
                    "dy_m": domain.get("dy"),
                    "source_resolution": f"{domain.get('dx')}m" if domain.get("dx") else None,
                    "create_time": now,
                    "update_time": now,
                })
    return rows


def _register_catalog(
    task_id: str,
    owner_sub: str,
    manifest: dict[str, Any],
    meta_path: Path,
    data_root: Path,
) -> dict[str, Any]:
    engine, _ = init_database(import_users=True)
    now = _utcnow()
    file_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"wrf-workbench:{task_id}"))
    manifest_payload = meta_path.read_bytes()
    file_hash = hashlib.sha256(manifest_payload).hexdigest()
    webp_files = list(meta_path.parent.rglob("*.webp"))
    total_size = len(manifest_payload) + sum(path.stat().st_size for path in webp_files)
    meta_relative = meta_path.relative_to(data_root).as_posix()
    default_url = next(
        (
            str(frame.get("url"))
            for domain in manifest.get("domains") or []
            for variable in domain.get("variables") or []
            for frame in variable.get("frames") or []
            if frame.get("url")
        ),
        None,
    )
    with engine.begin() as conn:
        owner_exists = bool(owner_sub) and conn.execute(
            select(users.c.uuid).where(users.c.uuid == owner_sub)
        ).first() is not None
        owner_uuid = owner_sub if owner_exists else None
        values = {
            "user_uuid": owner_uuid,
            "acquisition_type": "manual_import",
            "visibility": "private" if owner_uuid else "public",
            "data_type": "WRF",
            "file_type": "WEBP",
            "original_file_name": f"workbench_{task_id}",
            "stored_file_name": "scene.meta.json",
            "source_path": f"workbench/{task_id}/scene.meta.json",
            "file_size": total_size,
            "file_hash": file_hash,
            "ingest_status": "success",
            "parse_status": "success",
            "parse_attempts": 0,
            "parse_finished_at": now,
            "meta_path": meta_relative,
            "default_webp_url": default_url,
            "webp_count": len(webp_files),
            "adapter_name": "backend_wrf.renderer",
            "adapter_version": "1.2",
            "meta_schema_version": str(manifest.get("schema_version") or "1.2"),
            "is_pinned": False,
            "is_deleted": False,
            "download_count": 0,
            "update_time": now,
            "remark": f"workbench:{task_id}",
        }
        existing = conn.execute(select(public_info).where(public_info.c.file_uuid == file_uuid)).first()
        if existing is None:
            conn.execute(insert(public_info).values(file_uuid=file_uuid, create_time=now, **values))
        else:
            conn.execute(update(public_info).where(public_info.c.file_uuid == file_uuid).values(**values))
        conn.execute(delete(wrf_info).where(wrf_info.c.file_uuid == file_uuid))
        assets = _asset_rows(file_uuid, task_id, manifest, now)
        if assets:
            conn.execute(insert(wrf_info), assets)
    return {"file_uuid": file_uuid, "webp_count": len(webp_files), "meta_path": meta_relative}


def publish_workbench_result(
    task_id: str,
    owner_sub: str,
    source_dir: Path,
    manifest: dict[str, Any],
    data_root: Path,
) -> dict[str, Any]:
    if manifest.get("quality", {}).get("status") != "complete":
        return {"status": "skipped", "reason": "only complete WRF results enter the unified catalog"}
    target = data_root / "WRF" / "workbench" / task_id
    published_manifest = _rewrite_manifest_urls(manifest, task_id)
    payload = _manifest_bytes(published_manifest)
    created_target = False
    staging = target.parent / f".{task_id}.publishing"
    try:
        if target.exists():
            existing_meta = target / "scene.meta.json"
            if not existing_meta.is_file() or existing_meta.read_bytes() != payload:
                raise RuntimeError(f"WRF 统一目录已存在冲突结果：{target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(source_dir, staging)
            temporary_meta = staging / "scene.meta.json.part"
            temporary_meta.write_bytes(payload)
            temporary_meta.replace(staging / "scene.meta.json")
            staging.replace(target)
            created_target = True
        catalog = _register_catalog(task_id, owner_sub, published_manifest, target / "scene.meta.json", data_root)
        return {
            "status": "success",
            "source_origin": "workbench",
            "source_task_id": task_id,
            "meta_url": f"/data/WRF/workbench/{task_id}/scene.meta.json",
            **catalog,
        }
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if created_target:
            shutil.rmtree(target, ignore_errors=True)
        raise
