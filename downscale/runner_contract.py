from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .schemas import FORCING_DX_M, FORCING_SCHEMA_VERSION, FORCING_TOPOLOGY_ID, NOMINAL_FORCING_RESOLUTION_M, WRF_LES_PROFILE_ID, forcing_duration_hours, valid_times


RUNNER_TEMPLATE_ID = "wrf-cfd-managed-runner/v3"


def canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_runner_contract(task_id: str, request: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """The only job shape a remote WRF–CFD runner may consume.

    It intentionally contains typed scientific values and immutable artifact
    identities, never local paths, user commands, credentials or shell text.
    """
    source_artifact = runtime.get("source_wrf", {}).get("artifact") or {}
    source_wrf_runtime = runtime.get("source_wrf") or {}
    start = datetime.fromisoformat(str(request["start_time"]).replace("Z", "+00:00"))
    end_raw = request.get("end_time")
    end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")) if end_raw else None
    source_forcing = runtime.get("source_forcing") or {}
    source_artifact_identity = {
        key: source_artifact.get(key)
        for key in (
            "schema_version", "task_id", "final_domain", "final_dx_m",
            "history_file_count", "restart_file_count", "inventory_sha256",
            "source_center", "source_time_range", "source_domains",
            "history_times_utc", "restart_times_utc", "source_cycle", "source_forecast_hours",
            # Preserve the immutable native-file inventory for the remote
            # hand-off audit. The runner still resolves paths only through
            # the guarded data-volume layout.
            "remote_output_dir", "files",
        )
        if source_artifact.get(key) is not None
    }
    request_domains = ((request.get("wrf") or {}).get("domains") or [])
    final_dx = float((request_domains[-1] or {}).get("dx_m") or 0) if request_domains else 0.0
    production_forcing = request.get("upstream_mode") == "reuse_10m" or (
        request.get("upstream_mode") == "full_chain" and abs(final_dx - FORCING_DX_M) <= 0.02
    )
    return {
        "schema_version": "wrf-cfd-managed-job/v3",
        "runner_template_id": RUNNER_TEMPLATE_ID,
        "task_id": task_id,
        "execution_scope": request.get("execution_scope", "wrf_cfd"),
        "upstream": {
            "mode": request.get("upstream_mode"),
            "source_wrf_task_id": request.get("source_wrf_task_id") or source_wrf_runtime.get("task_id"),
            "start_time": request.get("start_time"),
            "end_time": request.get("end_time"),
            "wrf": request.get("wrf"),
            "artifact_id": request.get("upstream_artifact_id"),
            "content_sha256": request.get("upstream_content_sha256"),
            "source_artifact": source_artifact_identity,
            "source_forcing": {
                key: source_forcing.get(key)
                for key in ("task_id", "remote_output_dir", "manifest", "manifest_sha256", "vertical_datum")
                if source_forcing.get(key) is not None
            },
        },
        "execution_plan": runtime.get("execution_plan"),
        "terrain": runtime.get("terrain"),
        "surface": runtime.get("surface"),
        "parameterization": runtime.get("parameterization"),
        "configuration_hash": runtime.get("configuration_hash"),
        "publication_aoi": request.get("aoi"),
        "cfd": request.get("cfd") if request.get("execution_scope", "wrf_cfd") == "wrf_cfd" else None,
        "derived_domain": runtime.get("cfd_domain"),
        "sampling": runtime.get("microscale_sampling"),
        "forcing_contract": runtime.get("forcing"),
        "retention": runtime.get("retention"),
        "output_contract": (
            {
                "manifest": "mesoscale_forcing_manifest.json",
                "schema_version": FORCING_SCHEMA_VERSION,
                "topology_id": FORCING_TOPOLOGY_ID,
                "nominal_resolution_m": NOMINAL_FORCING_RESOLUTION_M,
                "horizontal_spacing_m": FORCING_DX_M,
                "valid_time_count": len(valid_times(start, end)),
                "valid_times_utc": valid_times(start, end),
                "duration_hours": forcing_duration_hours(start, end),
                "final_domain": "d08",
                "wrf_les_profile_id": WRF_LES_PROFILE_ID,
            }
            if request.get("execution_scope", "wrf_cfd") == "forcing_only" else
            {
                "manifest": "downscale.product.json", "schema_version": "downscale-product/v2",
                "standard_products": ["cf_netcdf", "geotiff_layers", "vtk_scene", "quality_report"],
                "continuous_1m_time_series": False,
                "mesoscale_forcing_schema_version": FORCING_SCHEMA_VERSION if production_forcing else "legacy-34m",
                "mesoscale_forcing_horizontal_spacing_m": FORCING_DX_M if production_forcing else 34.293556,
            }
        ),
    }


def stage_runner_contract(root: Path, task_id: str, request: dict[str, Any], runtime: dict[str, Any]) -> dict[str, str]:
    contract = build_runner_contract(task_id, request, runtime)
    payload = canonical_bytes(contract)
    target_dir = root / task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "downscale.job.json"
    staging = target.with_suffix(".json.staging")
    staging.write_bytes(payload)
    os.replace(staging, target)
    return {
        "schema_version": str(contract["schema_version"]),
        "runner_template_id": RUNNER_TEMPLATE_ID,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
