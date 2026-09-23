from __future__ import annotations

"""Validation helpers for the normal-WRF to high-resolution hand-off.

The high-resolution route consumes native WRF restart/history files.  A
published WebP, a scene manifest, or a task row without files is not an
initial condition and must never satisfy this gate.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any


_NORMAL_FILE = re.compile(r"^(wrf(?:out|rst)_d[0-9]{2}_.+)$")
_TIME = re.compile(r"_(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})$")


def _file_time(name: str) -> str | None:
    match = _TIME.search(name)
    if not match:
        return None
    try:
        value = datetime.strptime(match.group(1), "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return value.isoformat().replace("+00:00", "Z")


def build_source_artifact(
    source_task: dict[str, Any], inventory: dict[str, Any]
) -> dict[str, Any]:
    request = source_task.get("request") or {}
    domains = request.get("domains") or []
    final_domain = str((domains[-1] if domains else {}).get("id") or "").lower()
    final_dx_m = float((domains[-1] if domains else {}).get("dx") or 0)
    files = [item for item in (inventory.get("files") or []) if isinstance(item, dict)]
    files = [
        {
            "name": str(item.get("name") or ""),
            "kind": str(item.get("kind") or ""),
            "size_bytes": int(item.get("size_bytes") or 0),
            "mtime_utc": str(item.get("mtime_utc") or ""),
            "sha256": str(item.get("sha256") or "").lower(),
            "stable": bool(item.get("stable")),
        }
        for item in files
        if _NORMAL_FILE.fullmatch(str(item.get("name") or "")) and int(item.get("size_bytes") or 0) > 0
    ]
    final_files = [item for item in files if f"_{final_domain}_" in item["name"]]
    history = [item for item in final_files if item["kind"] == "history"]
    restart = [item for item in final_files if item["kind"] == "restart"]
    if not final_domain or final_dx_m <= 0:
        raise ValueError("普通 WRF 上游任务缺少最细域元数据")
    if not history:
        raise ValueError(f"普通 WRF 上游 {final_domain} 缺少 wrfout 历史场，不能交接")
    if not restart:
        raise ValueError(
            f"普通 WRF 上游 {final_domain} 缺少 wrfrst 重启场；请用启用每小时重启输出的新普通任务重跑"
        )
    unstable = [item["name"] for item in final_files if not item["stable"]]
    if unstable:
        raise ValueError(f"普通 WRF 上游仍有文件在写入：{', '.join(unstable[:5])}")
    # The high-resolution route receives only the d04 native hand-off. Keep
    # the inventory hash scoped to that exact list even if an older transport
    # endpoint returned d01-d03 files as well.
    final_files = sorted(final_files, key=lambda item: item["name"])
    final_inventory_payload = json.dumps(
        final_files, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    cache_key = "ecmwf_cache" if str(source_task.get("data_source") or request.get("data_source") or "").lower() == "ecmwf" else "gfs_cache"
    source_cache = request.get(cache_key) or {}
    runtime = source_task.get("runtime") or {}
    # The scheduler selects the actual cycle after task creation and records it
    # in runtime.  Auto-created upstream requests therefore do not carry the
    # legacy request.gfs_cache block.  Freeze the cycle that really produced
    # the source files; request cache metadata remains a historical fallback.
    runtime_cycle = str(
        runtime.get("forcing_cycle")
        or runtime.get("gfs_cycle")
        or runtime.get("ecmwf_cycle")
        or ""
    )
    cycle_date = str(source_cache.get("gfs_cycle_date") or source_cache.get("cycle_date") or "")
    cycle_hour = str(source_cache.get("gfs_cycle_hour") or source_cache.get("cycle_hour") or "")
    cached_cycle = (cycle_date + cycle_hour) if len(cycle_date) == 8 and len(cycle_hour) == 2 else ""
    source_cycle = runtime_cycle if len(runtime_cycle) == 10 and runtime_cycle.isdigit() else cached_cycle
    source_forecast_hours = (
        runtime.get("forecast_hours")
        or source_cache.get("gfs_forecast_hours")
        or source_cache.get("forecast_hours")
        or []
    )
    return {
        "schema_version": "normal-wrf-source-artifact/v1",
        "task_id": str(source_task.get("id") or ""),
        "status": "ready",
        "final_domain": final_domain,
        "final_dx_m": final_dx_m,
        "remote_output_dir": str(inventory.get("remote_output_dir") or ""),
        "source_center": request.get("center"),
        "source_time_range": {
            key: request.get(key) for key in ("start_time", "end_time") if request.get(key) is not None
        },
        "source_cycle": source_cycle,
        "source_forecast_hours": [int(hour) for hour in source_forecast_hours],
        "source_domains": domains,
        "history_file_count": len(history),
        "restart_file_count": len(restart),
        "history_times_utc": sorted({time for item in history if (time := _file_time(item["name"]))}),
        "restart_times_utc": sorted({time for item in restart if (time := _file_time(item["name"]))}),
        "files": final_files,
        "inventory_sha256": hashlib.sha256(final_inventory_payload).hexdigest(),
    }
