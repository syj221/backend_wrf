"""Recover legacy display geometry without overwriting original products."""

import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path

from renderer import render_run
from product_snapshot import bind_display
from runtime_assets import checked_path, read_run_path


_lock = threading.Lock()


def corrected_display(task: dict, settings) -> dict:
    return bind_display(task, _corrected_display(task, settings))


def _corrected_display(task: dict, settings) -> dict:
    result = deepcopy(task["result"])
    domains = (result.get("meta_json") or {}).get("domains") or []
    if not domains or all(domain.get("geolocation", {}).get("status") == "reprojected" for domain in domains):
        return result
    task_id = task["id"]
    raw_dir = read_run_path(settings, task, "raw")
    sources = sorted(checked_path(raw_dir, path.name) for path in raw_dir.glob("wrfout_d*_*"))
    if not sources:
        result["geolocation_repair"] = {"status": "unavailable", "message": "原始 wrfout 不在本地，无法恢复精确定位；请恢复原始输出后重试。"}
        return result
    signature = [(path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in sources]
    identity = json.dumps([signature, task["request"], "mercator-v1"], sort_keys=True).encode()
    version = hashlib.sha256(identity).hexdigest()[:16]
    directory = Path(settings.output_dir) / "runs" / task_id / "geolocated" / version
    prefix = f"/data/WRF/runs/{task_id}/geolocated/{version}"
    with _lock:
        try:
            manifest_path = directory / "scene.meta.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text())
            else:
                original = result["meta_json"]
                manifest = render_run(task_id, raw_dir, directory, task["request"], original.get("forcing_cycle") or original.get("gfs_cycle") or "",
                                      allow_partial=task["status"] == "partial_success", asset_prefix=prefix)
            result.update(meta_json=manifest, meta_url=prefix + "/scene.meta.json",
                          geolocation_repair={"status": "repaired", "original_meta_url": task["result"].get("meta_url")})
        except (OSError, ValueError, RuntimeError, KeyError):
            result["geolocation_repair"] = {"status": "failed", "message": "原始输出未通过定位或渲染校验，无法生成精确地图产品；请检查 WRF 输出。"}
    return result
