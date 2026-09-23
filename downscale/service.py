from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from catalog_publisher import publish_workbench_result
from config import Settings

from .product import atomically_publish_bundle, validate_product_bundle
from .profiles import (
    DEFAULT_PARAMETERIZATION_PROFILE_ID,
    PROFILE_SCHEMA_VERSION,
    parameterization_profile,
    parameterization_profiles,
    profile_recipe,
)
from .runner_contract import stage_runner_contract
from .schemas import (
    AGL_LEVELS_M,
    AUTO_FLAT_SURFACE_VARIABLES,
    AUTO_QUEUE_MAX_AOI_SIDE_M,
    DEFAULT_FORCING_DURATION_HOURS,
    FORCING_DX_M,
    FORCING_EDGE_HALO_M,
    FORCING_SCHEMA_VERSION,
    FORCING_TOPOLOGY_ID,
    FORCING_TIME_COUNT,
    LEGACY_FORCING_DX_M,
    LEGACY_REQUIRED_FORCING_VARIABLES,
    NOMINAL_FORCING_RESOLUTION_M,
    NORMAL_WRF_DX_M,
    REQUIRED_FORCING_VARIABLES,
    forcing_duration_hours,
    WRF_LES_PROFILE_ID,
    CertificationRequest,
    CertificationReview,
    DownscaleAoi,
    DownscaleTaskCreate,
    DownscaleValidationRequest,
    TerrainAsset,
    TerrainAssetReview,
    TerrainAssetSubmission,
    as_utc,
    valid_times,
)
from .store import DownscaleStore, utc_now
from .upstream import build_source_artifact


class DownscaleService:
    """Professional WRF–CFD task contract and local audit store.

    This service deliberately validates science/operations before any remote
    runner is invoked. It never accepts user-provided shell commands or paths.
    """

    def __init__(
        self,
        settings: Settings,
        normal_task_lookup: Callable[[str, str, int], dict[str, Any] | None] | None = None,
        source_artifact_lookup: Callable[[str, str, int], dict[str, Any] | None] | None = None,
        forcing_manifest_lookup: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        normal_task_submit: Callable[[dict[str, Any], str, str | None], dict[str, Any]] | None = None,
        normal_task_cancel: Callable[[str], dict[str, Any]] | None = None,
    ):
        self._submission_lock = threading.RLock()
        self.settings = settings
        self.store = DownscaleStore(settings.downscale_database_path)
        self.normal_task_lookup = normal_task_lookup
        self.source_artifact_lookup = source_artifact_lookup
        self.forcing_manifest_lookup = forcing_manifest_lookup
        self.normal_task_submit = normal_task_submit
        self.normal_task_cancel = normal_task_cancel

    def close(self) -> None:
        self.store.close()

    def terrain_assets(self) -> list[dict[str, Any]]:
        path = self.settings.downscale_terrain_catalog_path
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            items: list[dict[str, Any]] = []
            for raw in payload.get("items", []):
                # Earlier 520 m / STL-only records remain visible, but cannot
                # accidentally become production terrain releases.
                if raw.get("status") == "approved" and (
                    not raw.get("source_file_uuids") or not raw.get("asset_kinds")
                ):
                    raw = {**raw, "status": "smoke", "asset_kinds": raw.get("asset_kinds", []), "source_file_uuids": raw.get("source_file_uuids", [])}
                items.append(TerrainAsset.model_validate(raw).model_dump(mode="json"))
            return items
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"审核地形资产目录无效：{exc}") from exc

    @staticmethod
    def _terrain_distance_m(left_lat: float, left_lon: float, right_lat: float, right_lon: float) -> tuple[float, float]:
        """Local metric approximation used only for asset-coverage screening.

        Final CRS/datum checks remain hard gates in ``validate``. This gives the
        map an explainable early answer without pretending to replace an audited
        projected geometry engine.
        """
        meters_per_latitude = 111_320.0
        reference_lat = (left_lat + right_lat) / 2
        meters_per_longitude = meters_per_latitude * max(0.1, abs(math.cos(reference_lat * math.pi / 180)))
        return abs(left_lon - right_lon) * meters_per_longitude, abs(left_lat - right_lat) * meters_per_latitude

    def resolve_terrain_assets(self, aoi: DownscaleAoi) -> dict[str, Any]:
        """Return map-suitable reviewed terrain assets and explicit next actions."""
        required_kinds = {"dtm", "dsm", "buildings", "stl", "land_cover", "surface_properties"}
        candidates: list[dict[str, Any]] = []
        for asset in self.terrain_assets():
            reasons: list[str] = []
            hchar = float(asset.get("hchar_m", 0))
            buffer_m = max(300.0, 15.0 * hchar)
            required_width_m = float(aoi.width_m + 2 * buffer_m + 50.0)
            required_height_m = float(aoi.height_m + 2 * buffer_m + 50.0)
            if asset.get("status") != "approved":
                reasons.append("资产未通过审核")
            if asset.get("horizontal_crs") != aoi.horizontal_crs:
                reasons.append("审核 CRS 不匹配")
            missing = sorted(required_kinds - set(asset.get("asset_kinds") or []))
            if missing:
                reasons.append(f"缺少 {', '.join(missing)}")
            if float(asset.get("coverage_width_m", 0)) < required_width_m or float(asset.get("coverage_height_m", 0)) < required_height_m:
                reasons.append("地形覆盖尺寸不足")
            center_lat = asset.get("coverage_center_lat")
            center_lon = asset.get("coverage_center_lon")
            if center_lat is None or center_lon is None:
                reasons.append("资产尚未登记地图覆盖中心")
                coverage_status = "unlocated"
            else:
                delta_x, delta_y = self._terrain_distance_m(float(aoi.center_lat), float(aoi.center_lon), float(center_lat), float(center_lon))
                usable_half_width = max(0.0, (float(asset["coverage_width_m"]) - required_width_m) / 2)
                usable_half_height = max(0.0, (float(asset["coverage_height_m"]) - required_height_m) / 2)
                if delta_x > usable_half_width or delta_y > usable_half_height:
                    reasons.append("地图范围不在资产可用覆盖区内")
                    coverage_status = "outside"
                else:
                    coverage_status = "covered"
            suitability = "ready" if not reasons else ("reviewable" if asset.get("status") == "approved" and coverage_status == "unlocated" else "unavailable")
            candidates.append({
                **asset,
                "suitability": suitability,
                "coverage_status": coverage_status,
                "reasons": reasons,
                "required_coverage_width_m": required_width_m,
                "required_coverage_height_m": required_height_m,
                "derived_buffer_m": buffer_m,
            })
        ready = [item for item in candidates if item["suitability"] == "ready"]
        # Pick a stable default so the normal user flow only needs an AOI;
        # experts can still change the asset explicitly when several reviewed
        # releases cover the same rectangle.
        ready.sort(key=lambda item: (
            float(item.get("control_point_error_m") or 0),
            -(float(item.get("coverage_width_m") or 0) * float(item.get("coverage_height_m") or 0)),
            str(item.get("id") or ""),
        ))
        return {
            "items": candidates,
            "ready_asset_ids": [item["id"] for item in ready],
            "recommended_asset_id": ready[0]["id"] if ready else None,
            "required": {
                "asset_kinds": sorted(required_kinds),
                "aoi_crs": aoi.horizontal_crs,
                "aoi_center": {"lat": aoi.center_lat, "lon": aoi.center_lon},
            },
            "next_action": "select_approved_asset" if ready else "request_or_prepare_terrain",
            "forcing_only_available": True,
        }

    def _write_terrain_assets(self, items: list[dict[str, Any]]) -> None:
        target = self.settings.downscale_terrain_catalog_path
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(target.suffix + ".staging")
        staging.write_text(
            json.dumps({"schema_version": "downscale-terrain-assets/v2", "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(staging, target)

    def register_terrain_asset(self, payload: TerrainAsset) -> dict[str, Any]:
        """Admin-only registration of audited metadata; binary data remains in backend_upload."""
        items = self.terrain_assets()
        value = payload.model_dump(mode="json")
        items = [item for item in items if item["id"] != value["id"]]
        items.append(value)
        self._write_terrain_assets(items)
        return value

    def submit_terrain_asset(self, payload: TerrainAssetSubmission, owner_sub: str) -> dict[str, Any]:
        submission_id = f"terrain_{secrets.token_hex(8)}"
        value = payload.model_dump(mode="json")
        self.store.create_terrain_submission(submission_id, owner_sub, value)
        return {"submission_id": submission_id, "status": "pending_review", "asset": value}

    def review_terrain_asset(
        self, submission_id: str, payload: TerrainAssetReview, reviewer_sub: str
    ) -> dict[str, Any]:
        submission = self.store.terrain_submission(submission_id)
        if not submission:
            raise KeyError(submission_id)
        if submission["status"] != "pending_review":
            raise ValueError("该地形资产申请已完成审核")
        review = {"reviewer_sub": reviewer_sub, "note": payload.note, "reviewed_at": utc_now()}
        if not payload.approved:
            self.store.update_terrain_submission(submission_id, "rejected", review)
            return {"submission_id": submission_id, "status": "rejected", "review": review}
        submitted = dict(submission["payload"])
        mappings = list(submitted.get("asset_files") or [])
        if mappings:
            if len(self.settings.terrain_artifact_service_token) < 32:
                raise ValueError("真实地形审核需要配置内部资产服务凭据")
            enriched = []
            for item in mappings:
                url = f"{self.settings.terrain_artifact_base_url}/api/internal/terrain-assets/{item['file_uuid']}/manifest"
                request = urllib.request.Request(url, headers={"X-Weather-Service-Token": self.settings.terrain_artifact_service_token})
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        manifest = json.loads(response.read().decode("utf-8")).get("data") or {}
                except (OSError, ValueError, urllib.error.HTTPError) as exc:
                    raise ValueError(f"无法核验地形归档文件 {item['file_uuid']}：{exc}") from exc
                for key in ("sha256", "size_bytes", "file_name"):
                    if item.get(key) is not None and item[key] != manifest.get(key):
                        raise ValueError(f"地形归档文件 {item['file_uuid']} 的 {key} 已变化")
                enriched.append({**item, **manifest})
            submitted["asset_files"] = enriched
            submitted["source_file_uuids"] = [item["file_uuid"] for item in enriched]
            submitted["asset_kinds"] = [item["kind"] for item in enriched]
        asset = TerrainAsset(**submitted, status="approved")
        self.register_terrain_asset(asset)
        self.store.update_terrain_submission(submission_id, "approved", review)
        return {"submission_id": submission_id, "status": "approved", "asset": asset.model_dump(mode="json"), "review": review}

    def download_terrain_assets(self, surface: dict[str, Any], target: Path) -> list[tuple[Path, str]]:
        """Download immutable reviewed files into an isolated local staging directory."""
        if surface.get("mode") != "reviewed_terrain":
            return []
        if len(self.settings.terrain_artifact_service_token) < 32:
            raise ValueError("真实地形运行需要配置内部资产服务凭据")
        target.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[Path, str]] = []
        for item in surface.get("asset_files") or []:
            kind = str(item["kind"])
            suffix = Path(str(item.get("file_name") or "")).suffix.lower() or ".bin"
            local = target / f"{kind}{suffix}"
            url = f"{self.settings.terrain_artifact_base_url}/api/internal/terrain-assets/{item['file_uuid']}/content"
            request = urllib.request.Request(url, headers={"X-Weather-Service-Token": self.settings.terrain_artifact_service_token})
            digest = hashlib.sha256()
            try:
                with urllib.request.urlopen(request, timeout=120) as response, local.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk); digest.update(chunk)
            except (OSError, urllib.error.HTTPError) as exc:
                local.unlink(missing_ok=True)
                raise ValueError(f"无法下载地形归档文件 {item['file_uuid']}：{exc}") from exc
            if local.stat().st_size != int(item["size_bytes"]) or digest.hexdigest() != item["sha256"]:
                local.unlink(missing_ok=True)
                raise ValueError(f"地形归档文件 {item['file_uuid']} 下载后校验失败")
            staged.append((local, local.name))
        return staged

    def _terrain(self, asset_id: str) -> dict[str, Any]:
        for item in self.terrain_assets():
            if item["id"] == asset_id:
                return item
        raise ValueError("未找到审核地形资产；请先通过地形资产审核")

    def _source_wrf(self, request: DownscaleValidationRequest, owner_sub: str, owner_role: int) -> dict[str, Any]:
        source_id = str(request.source_wrf_task_id or "")
        if not source_id:
            if request.upstream_mode != "full_chain":
                raise ValueError("复用 forcing 的高分辨率任务必须绑定普通 WRF 上游任务")
            self._auto_upstream_request(request)
            # A one-click full chain creates the normal d04 upstream during
            # submission. Preflight can therefore validate the scientific
            # contract before that asynchronous task has an ID.
            return {"task_id": None, "status": "planned", "final_dx_m": NORMAL_WRF_DX_M, "auto_create": True, "owner_role": owner_role}
        if self.normal_task_lookup is None:
            raise ValueError("无法校验普通 WRF 上游任务")
        source = self.normal_task_lookup(source_id, owner_sub, owner_role)
        if not source:
            raise ValueError("指定的普通 WRF 上游任务不存在、无权访问或未完成")
        if source.get("status") != "succeeded":
            raise ValueError("普通 WRF 上游任务必须成功完成后才能进入高分降尺度")
        domains = (source.get("request") or {}).get("domains") or []
        final_dx = float((domains[-1] if domains else {}).get("dx") or 0)
        if abs(final_dx - 1000) > 0.001:
            current = f"{final_dx / 1000:g} km" if final_dx >= 1000 else f"{final_dx:g} m"
            raise ValueError(f"普通 WRF 上游任务最细域必须为 1 km，当前为 {current}")
        source_wrf = {"task_id": source_id, "status": source.get("status"), "final_dx_m": final_dx, "owner_role": owner_role}
        if self.source_artifact_lookup is not None:
            inventory = self.source_artifact_lookup(source_id, owner_sub, owner_role)
            if not inventory:
                raise ValueError("无法读取普通 WRF 上游原生输出；请检查远端任务目录和数据盘挂载")
            if inventory.get("error"):
                raise ValueError(str(inventory["error"]))
            source_wrf["artifact"] = build_source_artifact(source, inventory)
            if request.wrf.production_topology_ok():
                self._validate_source_compatibility(request, source_wrf["artifact"])
        return source_wrf

    @staticmethod
    def _validate_source_compatibility(request: DownscaleValidationRequest, artifact: dict[str, Any]) -> None:
        """Require the normal d04 geometry and time window before high WRF starts."""
        domains = artifact.get("source_domains") or []
        if len(domains) < 4:
            raise ValueError("普通 WRF 上游缺少 d01-d04 网格元数据，不能作为约 10 m forcing 父域")
        expected = [domain.model_dump() for domain in request.wrf.domains[:4]]
        actual = domains[:4]
        fields = (("dx_m", "dx"), ("dy_m", "dy"), ("e_we", "e_we"), ("e_sn", "e_sn"),
                  ("parent_start_i", "i_parent_start"), ("parent_start_j", "j_parent_start"))
        for index, (want, got) in enumerate(zip(expected, actual), 1):
            actual_id = str(got.get("id") or "").lower()
            if actual_id and actual_id != f"d{index:02d}":
                raise ValueError(f"普通 WRF 上游域名不匹配：需要 d{index:02d}，实际为 {actual_id}")
            for expected_key, actual_key in fields:
                if expected_key not in want or actual_key not in got:
                    raise ValueError(f"普通 WRF 上游 d{index:02d} 缺少 {actual_key} 网格参数")
                tolerance = 0.01 if expected_key in {"dx_m", "dy_m"} else 0
                if abs(float(want[expected_key]) - float(got[actual_key])) > tolerance:
                    raise ValueError(
                        f"普通 WRF 上游 d{index:02d} 网格与高分合同不一致：{actual_key}={got[actual_key]}，需要 {want[expected_key]}"
                    )
        if str(artifact.get("final_domain") or "").lower() != "d04" or abs(float(artifact.get("final_dx_m") or 0) - NORMAL_WRF_DX_M) > 0.01:
            raise ValueError("约 10 m forcing 只允许使用普通 WRF d04 · 1 km 原生输出")
        source_times = artifact.get("source_time_range") or {}
        if source_times.get("start_time"):
            source_start = as_utc(datetime.fromisoformat(str(source_times["start_time"]).replace("Z", "+00:00")))
            if source_start != as_utc(request.start_time):
                raise ValueError("普通 WRF 上游起报时间与约 10 m forcing 任务不一致，请从匹配的普通任务重新创建")
        history_times = artifact.get("history_times_utc") or []
        restart_times = artifact.get("restart_times_utc") or []
        expected_times = valid_times(request.start_time, request.end_time)
        required_count = len(expected_times)
        if history_times and len(history_times) < required_count:
            raise ValueError(f"普通 WRF d04 原生历史场不足 {required_count} 个逐小时文件，不能覆盖当前 forcing 窗口")
        if restart_times and len(restart_times) < required_count:
            raise ValueError(f"普通 WRF d04 原生重启场不足 {required_count} 个逐小时文件，不能安全恢复高分链")
        if history_times and not set(expected_times).issubset(set(history_times)):
            raise ValueError("普通 WRF d04 历史场时次不连续或与高分任务起报时间不匹配")
        if restart_times and not set(expected_times).issubset(set(restart_times)):
            raise ValueError("普通 WRF d04 重启场时次不连续或与高分任务起报时间不匹配")

    def _source_forcing(self, request: DownscaleValidationRequest, owner_sub: str, owner_role: int) -> dict[str, Any]:
        """Load the immutable d08 hand-off for the second, CFD-only phase."""
        source_id = str(request.source_forcing_task_id or "")
        task = self.store.get(source_id)
        if not task or (owner_role < 3 and task.get("owner_sub") != owner_sub):
            raise ValueError("指定的约 10 m forcing 任务不存在、无权访问或未完成")
        if task.get("status") != "succeeded" or (task.get("request") or {}).get("execution_scope") != "forcing_only":
            raise ValueError("约 10 m CFD 任务必须基于已成功完成的 forcing-only 任务")
        task_request = task.get("request") or {}
        if (task_request.get("upstream_mode") or "") != "full_chain":
            raise ValueError("forcing 上游任务不是完整 d04→d08 受管链路")
        lineage = ((task.get("runtime") or {}).get("source_wrf") or {}).get("task_id")
        if lineage and lineage != request.source_wrf_task_id:
            raise ValueError("普通 WRF 上游与约 10 m forcing 的谱系不一致")

        runtime = task.get("runtime") or {}
        remote_output_dir = str(runtime.get("remote_output_dir") or ((runtime.get("forcing") or {}).get("remote_output_dir") or ""))
        expected_output_dir = f"{self.settings.hpc_storage_alias.rstrip('/')}/WRFwork/DOWNSCALE_{source_id}/run"
        if remote_output_dir != expected_output_dir:
            raise ValueError("约 10 m forcing 没有位于受管数据盘任务目录，拒绝把系统盘路径交给 CFD")
        manifest = runtime.get("forcing_manifest")
        if not isinstance(manifest, dict) and self.forcing_manifest_lookup is not None:
            manifest = self.forcing_manifest_lookup(task)
        if not isinstance(manifest, dict):
            raise ValueError("无法读取约 10 m forcing manifest；请先重新校验 forcing 任务")
        return {
            "task_id": source_id,
            "remote_output_dir": remote_output_dir,
            "manifest": manifest,
            "owner_sub": task.get("owner_sub"),
        }

    @staticmethod
    def _validate_10m_forcing_manifest(
        request: DownscaleValidationRequest, domain: dict[str, Any], source: dict[str, Any]
    ) -> dict[str, Any]:
        manifest = source["manifest"]
        if manifest.get("schema_version") != FORCING_SCHEMA_VERSION or manifest.get("topology_id") != FORCING_TOPOLOGY_ID:
            raise ValueError("CFD 只能接收 mesoscale-forcing/v2 的新约 10 m forcing，旧 34 m 资产不兼容")
        if manifest.get("final_domain") != "d08" or int(manifest.get("domain_count") or 0) != 8:
            raise ValueError("约 10 m forcing manifest 缺少完整 d01-d08 拓扑")
        try:
            spacing = float(manifest.get("horizontal_spacing_m"))
        except (TypeError, ValueError):
            spacing = 0.0
        if abs(spacing - FORCING_DX_M) > 0.02 or int(manifest.get("nominal_resolution_m") or 0) != NOMINAL_FORCING_RESOLUTION_M:
            raise ValueError("forcing 的实际网格间距不是约 10 m d08 · 12.345679 m")
        if manifest.get("quality_status") != "mesoscale_forcing_complete" or (manifest.get("quality") or {}).get("status") != "pass":
            raise ValueError("约 10 m forcing 尚未通过完整质量门")
        expected_times = valid_times(request.start_time, request.end_time)
        if manifest.get("valid_times_utc") != expected_times:
            raise ValueError("约 10 m forcing 必须精确覆盖当前 CFD 任务的起止时间和逐小时有效时次")
        entries = manifest.get("forcing_files") or []
        if len(entries) != len(expected_times):
            raise ValueError(f"约 10 m forcing 文件清单不是完整的 {len(expected_times)} 个时次")
        for expected_time, entry in zip(expected_times, entries):
            if not isinstance(entry, dict) or entry.get("valid_time_utc") != expected_time:
                raise ValueError("约 10 m forcing 文件清单存在缺失、重复或错序时次")
            path = str(entry.get("path") or "")
            if not path or path.startswith("/") or ".." in Path(path).parts:
                raise ValueError("约 10 m forcing 清单包含非法路径")
            if int(entry.get("size_bytes") or 0) <= 0 or len(str(entry.get("sha256") or "")) != 64:
                raise ValueError("约 10 m forcing 清单缺少文件大小或 SHA-256")
        vertical = manifest.get("vertical") or {}
        if int(manifest.get("vertical_layer_count") or vertical.get("layer_count") or 0) < 88:
            raise ValueError("约 10 m forcing 垂直层数不足 88 层")
        try:
            top_height = float(vertical.get("top_height_m") or 0)
        except (TypeError, ValueError):
            top_height = 0.0
        if top_height < float(domain.get("z_top_m_agl") or 0):
            raise ValueError("约 10 m forcing 垂直范围不足以覆盖 CFD 顶部")
        variables = {str(value).upper() for value in (manifest.get("variables") or [])}
        turbulence = {"TKE", "QKE"} & variables
        if not turbulence:
            raise ValueError("约 10 m CFD 入口必须包含 WRF TKE/QKE，禁止使用固定湍流强度回退")
        required = {"U", "V", "W", "T", "P", "PB", "PH", "PHB", "QVAPOR", "HGT", "UST", "HFX", "PBLH"}
        if not required.issubset(variables):
            raise ValueError("约 10 m forcing 缺少 CFD 边界必需变量")
        coverage = manifest.get("coverage") or {}
        extent = coverage.get("final_extent_m") or {}
        try:
            extent_x, extent_y = float(extent.get("x") or 0), float(extent.get("y") or 0)
        except (TypeError, ValueError):
            extent_x = extent_y = 0.0
        # d08 was built for the publication AOI plus a 500 m outer halo.  The
        # CFD domain is already derived from that AOI, so the CFD hand-off
        # only needs a small d08 interpolation margin at its perimeter.
        required_x = float(domain["width_m"]) + 2 * FORCING_DX_M
        required_y = float(domain["height_m"]) + 2 * FORCING_DX_M
        if extent_x < required_x or extent_y < required_y:
            raise ValueError("约 10 m d08 覆盖不足以包住 CFD 外域及边界插值余量")
        if manifest.get("horizontal_crs") and manifest.get("horizontal_crs") != request.aoi.horizontal_crs:
            raise ValueError("约 10 m forcing CRS 与 CFD AOI 不一致")
        if source.get("vertical_datum") and manifest.get("vertical_datum") and manifest.get("vertical_datum") != source.get("vertical_datum"):
            raise ValueError("约 10 m forcing 垂直基准与审核地形不一致")
        manifest_sha256 = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "mode": "reuse_10m", "status": "eligible", "schema_version": FORCING_SCHEMA_VERSION,
            "topology_id": FORCING_TOPOLOGY_ID, "task_id": source["task_id"],
            "remote_output_dir": source["remote_output_dir"], "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "source_forcing": {**source, "manifest_sha256": manifest_sha256},
            "required_halo_m": FORCING_EDGE_HALO_M,
        }

    @staticmethod
    def _canonical(request: DownscaleValidationRequest) -> dict[str, Any]:
        value = request.model_dump(mode="json", exclude_none=True)
        value.pop("confirmation_token", None)
        return value

    @staticmethod
    def _parameterization(request: DownscaleValidationRequest) -> dict[str, Any] | None:
        """Resolve server-owned profile metadata for the typed request."""
        if request.execution_scope != "wrf_cfd":
            return None
        profile_id = request.parameterization_profile_id
        if not profile_id:
            profile_id = DEFAULT_PARAMETERIZATION_PROFILE_ID
        if not profile_id:
            return None
        profile = parameterization_profile(profile_id)
        final = {
            "wrf": request.wrf.model_dump(mode="json"),
            "cfd": request.cfd.model_dump(mode="json"),
        }
        expected = profile_recipe(profile)
        serialized = json.dumps(final, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        expected_serialized = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "id": profile["id"],
            "version": profile["version"],
            "label": profile["label"],
            "description": profile["description"],
            "use_cases": profile["use_cases"],
            "certification_status": profile["certification_status"],
            "release_mode": profile["release_mode"],
            "recommended": profile["recommended"],
            "editable_groups": profile["editable_groups"],
            "fixed_notes": profile["fixed_notes"],
            "resource_estimate": profile["resource_estimate"],
            "output_type": profile["output_type"],
            "manual_review_required": profile["manual_review_required"],
            "formal_release_allowed": profile["formal_release_allowed"],
            "final_parameters": final,
            "editable_parameters": profile["editable_parameters"],
            "fixed_parameters": profile["fixed_parameters"],
            "profile_recipe_sha256": hashlib.sha256(expected_serialized).hexdigest(),
            "final_recipe_sha256": hashlib.sha256(serialized).hexdigest(),
            "customized": final != expected,
            "configuration_status": "customized_research" if final != expected else "profile_default",
        }

    @staticmethod
    def _recipe(request: DownscaleValidationRequest) -> dict[str, Any]:
        """Scientific settings only; AOI/time/terrain are task inputs, not a recipe profile."""
        profile_id = request.parameterization_profile_id or (
            DEFAULT_PARAMETERIZATION_PROFILE_ID if request.execution_scope == "wrf_cfd" else None
        )
        recipe = {
            "schema_version": "wrf-cfd-scientific-recipe/v3",
            "execution_scope": request.execution_scope,
            "surface_mode": request.surface_mode if request.execution_scope == "wrf_cfd" else None,
            "parameterization_schema_version": PROFILE_SCHEMA_VERSION if request.execution_scope == "wrf_cfd" else None,
            "parameterization_profile_id": profile_id,
            "wrf": request.wrf.model_dump(mode="json"),
        }
        if request.execution_scope == "wrf_cfd":
            recipe["cfd"] = request.cfd.model_dump(mode="json")
            profile = parameterization_profile(profile_id)
            recipe["parameterization_profile_recipe_sha256"] = hashlib.sha256(
                json.dumps(profile_recipe(profile), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        return recipe

    @classmethod
    def _recipe_hash(cls, request: DownscaleValidationRequest) -> str:
        payload = json.dumps(cls._recipe(request), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()

    def _certification(self, request: DownscaleValidationRequest) -> dict[str, Any]:
        recipe_hash = self._recipe_hash(request)
        if not request.certification_profile_id:
            return {"status": "research", "recipe_hash": recipe_hash, "reason": "尚未冻结为认证配置"}
        profile = self.store.certification_profile(request.certification_profile_id)
        if not profile or profile["recipe_hash"] != recipe_hash:
            raise ValueError("认证配置不存在，或与当前 WRF/CFD 科学参数不精确匹配")
        return {"status": "certified", "profile_id": profile["id"], "recipe_hash": recipe_hash}

    @staticmethod
    def _execution_plan(request: DownscaleValidationRequest, source_wrf: dict[str, Any]) -> dict[str, Any]:
        domains = [domain.model_dump(mode="json") for domain in request.wrf.domains]
        source_final_dx = float(source_wrf.get("final_dx_m") or 0)
        if request.upstream_mode == "reuse_10m":
            return {
                "schema_version": "wrf-cfd-execution-plan/v3",
                "stages": [
                    "source_forcing_handoff", "surface_prepare", "cfd_1m_interface", "cfd_1m_mesh",
                    "cfd_1m_solver", "cfd_1m_export", "cfd_product_ready",
                ],
                "source": {
                    "task_id": source_wrf.get("task_id"),
                    "forcing_task_id": request.source_forcing_task_id,
                    "final_dx_m": source_final_dx,
                    "forcing_schema_version": FORCING_SCHEMA_VERSION,
                    "forcing_domain": "d08",
                    "forcing_manifest_required": True,
                },
                "cfd": {
                    "input_dx_m": FORCING_DX_M,
                    "solver_family": request.cfd.solver_family,
                    "publication_sampling_m": request.cfd.domain.surface_refinement_m,
                    "forcing_handoff": "mesoscale-forcing/v2",
                },
            }
        bridge = "source_d04_restart_to_d08"
        stages = [
            "source_handoff",
            "ndown_d04_d05",
            "wrf_d05_333m",
            "ndown_d05_d06",
            "wrf_d06_111m",
            "ndown_d06_d07",
            "wrf_d07_37m",
            "ndown_d07_d08",
            "wrf_d08_10m",
            "forcing_ready",
        ]
        if request.execution_scope == "wrf_cfd" and request.upstream_mode == "full_chain" and request.wrf.production_topology_ok():
            stages.extend(["source_forcing_handoff", "surface_prepare", "cfd_1m_interface", "cfd_1m_mesh", "cfd_1m_solver", "cfd_1m_export", "cfd_product_ready"])
        return {
            "schema_version": "wrf-cfd-execution-plan/v4" if request.execution_scope == "wrf_cfd" and request.upstream_mode == "full_chain" and request.wrf.production_topology_ok() else "wrf-les-forcing-execution-plan/v2",
            "stages": stages,
            "source": {
                "task_id": source_wrf.get("task_id"),
                "final_dx_m": source_final_dx,
                "artifact_required": True,
                "restart_required": True,
            },
            "wrf_les": {
                "domains": domains,
                "target_dx_m": request.wrf.final_dx_m,
                "nominal_resolution_m": NOMINAL_FORCING_RESOLUTION_M,
                "topology_id": FORCING_TOPOLOGY_ID,
                "profile_id": WRF_LES_PROFILE_ID,
                "transition_domain": "d05",
                "parent_domain": "d04",
                "source_parent_required": True,
                "physics": {
                    "bl_pbl_physics": 0,
                    "diff_opt": 2,
                    "km_opt": 2,
                    "cu_physics": 0,
                    "e_vert": request.wrf.e_vert,
                    "history_interval_minutes": 60,
                    "restart_interval_minutes": 60,
                },
                "bridge": bridge,
                "ndown_template_id": request.wrf.ndown_template_id,
            },
            "cfd": None if request.execution_scope == "forcing_only" else {
                "input_dx_m": request.wrf.final_dx_m,
                "solver_family": request.cfd.solver_family,
                "publication_sampling_m": request.cfd.domain.surface_refinement_m,
                "composite_same_task": request.upstream_mode == "full_chain" and request.wrf.production_topology_ok(),
            },
        }

    def _derive_domain(self, request: DownscaleValidationRequest, terrain: dict[str, Any]) -> dict[str, Any]:
        hchar = float(terrain["hchar_m"])
        minimum_buffer = max(300.0, 15.0 * hchar)
        requested_buffer = request.cfd.domain.buffer_m
        buffer_m = float(requested_buffer if requested_buffer is not None else minimum_buffer)
        if buffer_m < minimum_buffer:
            raise ValueError(f"CFD 缓冲不得小于 {minimum_buffer:.1f} m（max(300 m, 15×Hchar)）")
        minimum_top = max(384.0, 8.0 * hchar)
        requested_top = request.cfd.domain.z_top_m_agl
        z_top_m = float(requested_top if requested_top is not None else minimum_top)
        if z_top_m < minimum_top:
            raise ValueError(f"CFD 顶部不得低于 {minimum_top:.1f} m AGL")
        if max(request.cfd.publication_heights_agl_m) > z_top_m:
            raise ValueError("发布高度不得高于 CFD 顶部")
        width = float(request.aoi.width_m + 2 * buffer_m)
        height = float(request.aoi.height_m + 2 * buffer_m)
        required_width = width + 50.0
        required_height = height + 50.0
        if float(terrain["coverage_width_m"]) < required_width or float(terrain["coverage_height_m"]) < required_height:
            raise ValueError("审核地形未覆盖 CFD 外域及 25 m 外扩边界")

        # Calibrated after the 3 m/1 m RSS campaign. This is a conservative gate,
        # not an automatic request to coarsen the mesh.
        estimated_cells = max(500_000, int(10_500_000 * (width * height) / (1_100 * 1_100)))
        estimated_memory_gib = round(estimated_cells * 1_536 / (1024**3), 1)
        estimated_disk_gib = max(100.0, round(estimated_cells * 6_144 / (1024**3), 1))
        review_reasons: list[str] = []
        if request.aoi.longest_side_m > AUTO_QUEUE_MAX_AOI_SIDE_M:
            review_reasons.append("AOI 最长边超过 2 km")
        if buffer_m > 500:
            review_reasons.append("地形推导缓冲超过 500 m")
        if estimated_cells > 14_000_000:
            review_reasons.append("预测网格超过 14M cells，需高内存队列")
        if estimated_memory_gib > self.settings.downscale_available_memory_gib * 0.75:
            review_reasons.append("预测峰值内存超过可用内存的 75%")
        if estimated_disk_gib > self.settings.downscale_available_disk_gib:
            review_reasons.append("预测磁盘需求超过当前受管资源")
        return {
            "buffer_m": buffer_m,
            "minimum_buffer_m": minimum_buffer,
            "width_m": width,
            "height_m": height,
            "z_top_m_agl": z_top_m,
            "minimum_z_top_m_agl": minimum_top,
            "mesh_profile": "user-configured-graded/v2",
            "mesh_bands": [band.model_dump(mode="json") for band in request.cfd.domain.mesh_bands],
            "transition_cells": request.cfd.domain.transition_cells,
            "estimated_cells": estimated_cells,
            "estimated_memory_gib": estimated_memory_gib,
            "estimated_disk_gib": estimated_disk_gib,
            "review_reasons": review_reasons,
            "queue_mode": "review" if review_reasons else "automatic",
        }

    def _surface_inputs(self, request: DownscaleValidationRequest) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Resolve the user-facing surface choice into audited runner inputs."""
        if request.execution_scope == "forcing_only":
            return None, None
        if request.surface_mode == "auto_flat":
            synthetic = {
                "id": "managed-auto-flat/v1", "version": "v1", "status": "approved",
                "checksum_sha256": hashlib.sha256(b"managed-auto-flat/v1").hexdigest(),
                "horizontal_crs": request.aoi.horizontal_crs, "vertical_datum": "local-flat-agl",
                "coverage_width_m": 1_000_000_000.0, "coverage_height_m": 1_000_000_000.0,
                "hchar_m": 0.0, "control_point_error_m": 0.0,
            }
            return synthetic, {
                "schema_version": "wrf-cfd-surface/v1", "mode": "auto_flat",
                "geometry": {"kind": "generated_flat_prism", "version": "flat-surface/v1", "elevation_agl_m": 0.0},
                "roughness": {"method": "wrf_lu_index_znt_median/v1", "status": "pending_forcing", "required_variables": ["LU_INDEX", "ZNT"]},
                "vertical_reference": "local-flat-agl", "limitations": ["no_real_topography", "no_buildings"],
            }
        terrain = self._terrain(str(request.terrain_asset_id))
        if terrain["status"] != "approved":
            raise ValueError("地形资产尚未通过正式审核；smoke 地形不可用于生产或研究运行")
        if terrain["horizontal_crs"] != request.aoi.horizontal_crs:
            raise ValueError("AOI CRS 与审核地形水平 CRS 不一致")
        if float(terrain["control_point_error_m"]) > 0.5:
            raise ValueError("地形控制点误差超过 0.5 m")
        if not terrain.get("asset_files"):
            raise ValueError("该历史地形资产没有不可变文件映射，需重新提交审核后才能用于 CFD")
        return terrain, {
            "schema_version": "wrf-cfd-surface/v1", "mode": "reviewed_terrain",
            "terrain_asset_id": terrain["id"], "version": terrain["version"],
            "checksum_sha256": terrain["checksum_sha256"], "vertical_reference": terrain["vertical_datum"],
            "asset_files": terrain["asset_files"],
        }

    def _forcing_check(
        self, request: DownscaleValidationRequest, domain: dict[str, Any] | None, terrain: dict[str, Any] | None,
        owner_sub: str, owner_role: int = 1,
    ) -> dict[str, Any]:
        if request.upstream_mode == "reuse_10m":
            if domain is None or terrain is None:
                raise ValueError("约 10 m forcing → CFD 交接必须具有地表模型与 CFD 外域")
            source = self._source_forcing(request, owner_sub, owner_role)
            if request.surface_mode == "reviewed_terrain":
                source["vertical_datum"] = terrain.get("vertical_datum")
            return self._validate_10m_forcing_manifest(request, domain, source)
        if request.upstream_mode == "full_chain":
            production = request.wrf.production_topology_ok()
            legacy = (
                len(request.wrf.domains) == 7
                and abs(request.wrf.domains[0].dx_m - 25_000.0) < 0.01
                and abs(request.wrf.final_dx_m - LEGACY_FORCING_DX_M) < 0.02
            )
            if not production and not (legacy and request.execution_scope == "wrf_cfd"):
                raise ValueError("约 10 m forcing 必须使用固定的 27 km→9 km→3 km→1 km→333 m→111 m→37 m→12.345679 m 拓扑")
            if legacy:
                return {
                    "mode": "full_chain", "status": "planned", "topology_status": "legacy_34m",
                    "required_valid_times": valid_times(request.start_time, request.end_time),
                    "required_variables": sorted(LEGACY_REQUIRED_FORCING_VARIABLES),
                    "required_vertical_layers": 88, "ndown_required": True, "final_wrf_required": True,
                }
            topology = "production_candidate"
            coverage = request.wrf.production_coverage(request.aoi.width_m, request.aoi.height_m)
            if not coverage["valid"]:
                if coverage.get("final_extent_m", {}).get("x", 0) < coverage.get("required_extent_m", {}).get("x", 0) or coverage.get("final_extent_m", {}).get("y", 0) < coverage.get("required_extent_m", {}).get("y", 0):
                    raise ValueError("AOI 加每侧 500 m 外扩超出固定 d08 · 12.345679 m forcing 覆盖范围")
                raise ValueError("高分 forcing 的每一级父域边界缓冲不足 10 个父网格单元")
            if request.execution_scope == "wrf_cfd" and domain is not None:
                final_extent = coverage.get("final_extent_m") or {}
                if float(final_extent.get("x") or 0) < float(domain["width_m"]) + 2 * FORCING_DX_M or float(final_extent.get("y") or 0) < float(domain["height_m"]) + 2 * FORCING_DX_M:
                    raise ValueError("固定 d08 forcing 覆盖不足以包住 CFD 支撑域；请缩小 AOI 或使用更大 forcing 域")
            return {
                "mode": "full_chain",
                "status": "planned",
                "topology_status": topology,
                "required_valid_times": valid_times(request.start_time, request.end_time),
                "required_variables": sorted(REQUIRED_FORCING_VARIABLES | (AUTO_FLAT_SURFACE_VARIABLES if request.surface_mode == "auto_flat" and request.execution_scope == "wrf_cfd" else set())),
                "required_vertical_layers": 88,
                "ndown_required": True,
                "final_wrf_required": True,
                "coverage": request.wrf.production_coverage(request.aoi.width_m, request.aoi.height_m),
            }

        if domain is None or terrain is None:
            raise ValueError("复用 forcing 只能用于具有审核地形与 CFD 外域的 WRF–CFD 任务")
        if abs(request.wrf.final_dx_m - LEGACY_FORCING_DX_M) > 0.02:
            raise ValueError("复用已有 forcing 只支持历史 34.293556 m 资产；约 10 m forcing 必须重新执行 d04→d08 链")
        artifact = self.store.forcing(str(request.upstream_artifact_id))
        if not artifact or artifact["content_sha256"] != request.upstream_content_sha256:
            raise ValueError("34 m forcing artifact 不存在或内容哈希不匹配")
        manifest = artifact["manifest"]
        certified = manifest.get("certification", {}).get("status") == "certified" or manifest.get("certification_status") == "certified"
        if manifest.get("quality_status") != "mesoscale_forcing_complete" or not certified:
            raise ValueError("复用 forcing 必须是通过质量门且已认证的 mesoscale forcing artifact")
        if artifact["owner_sub"] != owner_sub and not bool(manifest.get("shareable")):
            raise ValueError("该 forcing artifact 未向当前用户共享")
        if manifest.get("valid_times_utc") != valid_times(request.start_time, request.end_time):
            raise ValueError("复用 forcing 必须精确覆盖同一高分任务的起止时间和逐小时有效时次")
        required_width = domain["width_m"] + 6 * LEGACY_FORCING_DX_M
        required_height = domain["height_m"] + 6 * LEGACY_FORCING_DX_M
        if float(manifest.get("coverage_width_m", 0)) < required_width or float(manifest.get("coverage_height_m", 0)) < required_height:
            raise ValueError("复用 forcing 未覆盖 CFD 外域及每侧 3 个 34 m 格点 halo")
        if float(manifest.get("vertical_top_m_agl", 0)) < domain["z_top_m_agl"]:
            raise ValueError("复用 forcing 未覆盖 CFD 顶部")
        if int(manifest.get("vertical_layer_count", 0)) < 88 or int(manifest.get("layers_above_cfd_top", 0)) < 2:
            raise ValueError("复用 forcing 的垂直层数或 CFD 顶部余量不足")
        variables = {str(value).upper() for value in manifest.get("variables", [])}
        if not LEGACY_REQUIRED_FORCING_VARIABLES.issubset(variables):
            raise ValueError("复用 forcing 缺少 WRF–CFD 边界必需变量")
        if manifest.get("horizontal_crs") != request.aoi.horizontal_crs or manifest.get("vertical_datum") != terrain["vertical_datum"]:
            raise ValueError("复用 forcing 的 CRS 或垂直基准与任务不匹配")
        return {
            "mode": "reuse_34m", "status": "eligible", "artifact_id": artifact["artifact_id"],
            "content_sha256": artifact["content_sha256"], "required_halo_m": 3 * LEGACY_FORCING_DX_M,
        }

    def _sampling_preview(self, request: DownscaleValidationRequest) -> dict[str, Any]:
        times = valid_times(request.start_time, request.end_time)
        if request.cfd.solver_family == "rans":
            assert request.cfd.rans is not None
            selected = [times[hour] for hour in request.cfd.rans.selected_hours]
            recommendation_hours = sorted(set([0, 12, 24, 36, 48, len(times) - 1]))
            recommendation = [times[hour] for hour in recommendation_hours if hour < len(times)]
            return {
                "solver_family": "rans", "temporal_semantics": "independent_representative_steady_snapshots",
                "continuous_1m_time_series": False, "selected_valid_times": selected,
                "system_recommendation": recommendation, "max_count": 6,
            }
        assert request.cfd.les is not None
        return {
            "solver_family": "les", "temporal_semantics": "finite_les_time_window_statistics",
            "continuous_1m_time_series": False, "episodes": [
                {**episode.model_dump(mode="json"), "forcing_valid_time": times[episode.forcing_hour]}
                for episode in request.cfd.les.episodes
            ], "max_count": 2,
        }

    def validate(self, request: DownscaleValidationRequest, owner_sub: str, owner_role: int = 1) -> dict[str, Any]:
        parameterization = self._parameterization(request)
        source_wrf = self._source_wrf(request, owner_sub, owner_role)
        is_forcing_only = request.execution_scope == "forcing_only"
        terrain: dict[str, Any] | None = None
        surface: dict[str, Any] | None = None
        domain: dict[str, Any] | None = None
        if not is_forcing_only:
            terrain, surface = self._surface_inputs(request)
            domain = self._derive_domain(request, terrain)
        forcing = self._forcing_check(request, domain, terrain, owner_sub, owner_role)
        certification = self._certification(request)
        if forcing.get("topology_status") == "research_topology" and domain is not None:
            domain["review_reasons"].append("WRF 嵌套拓扑偏离固定 27 km→约 10 m forcing 结构")
            domain["queue_mode"] = "review"
        canonical = self._canonical(request)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.settings.downscale_confirmation_ttl_seconds)).isoformat().replace("+00:00", "Z")
        nonce = secrets.token_urlsafe(24)
        message = json.dumps({"owner": owner_sub, "request": canonical, "nonce": nonce, "expires_at": expires_at}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self.settings.downscale_confirmation_secret.encode(), message, hashlib.sha256).hexdigest()
        token = f"{nonce}.{signature}"
        self.store.save_confirmation(hashlib.sha256(token.encode()).hexdigest(), owner_sub, canonical, expires_at)
        duration = forcing_duration_hours(request.start_time, request.end_time)
        warnings = [] if is_forcing_only else [
            "1 m 表示发布采样间距，不表示全域均匀 CFD 体网格。",
            f"RANS 快照或 LES 统计时段不得被展示为连续 {duration} h 的 1 m 预报。",
        ]
        if is_forcing_only:
            warnings.append(f"该任务仅生成约 10 m（实际 12.345679 m）、{duration} h / {duration + 1} 时次三维 forcing；不会启动 CFD，也不会发布 1 m 产品。")
        if not is_forcing_only and request.cfd.turbulence.source == "diagnostic_closure":
            warnings.append("将强制检查 UST、HFX、PBLH；缺项时不会回退到固定湍流强度。")
        if certification["status"] != "certified":
            warnings.append("当前为自定义研究配置；完成后只能内部预览，需专家审核冻结配置哈希后才可正式发布。")
        if not is_forcing_only and request.surface_mode == "auto_flat":
            warnings.append("自动平坦地表不表达真实坡度与建筑，产品将标记为理想化地表内部预览。")
        return {
            "confirmation_token": token,
            "expires_at": expires_at,
            "request": canonical,
            "product": ({
                "kind": "mesoscale_forcing_only", "topology_id": FORCING_TOPOLOGY_ID,
                "nominal_resolution_m": NOMINAL_FORCING_RESOLUTION_M, "horizontal_spacing_m": FORCING_DX_M,
                "valid_time_count": len(valid_times(request.start_time, request.end_time)), "continuous_1m_time_series": False,
            } if is_forcing_only else {
                "width_m": request.aoi.width_m, "height_m": request.aoi.height_m, "sampling_m": 1,
                "agl_levels_m": request.cfd.publication_heights_agl_m,
            }),
            "cfd_domain": domain,
            "forcing": forcing,
            "sampling": None if is_forcing_only else self._sampling_preview(request),
            "certification": certification,
            "parameterization": parameterization,
            "source_wrf": source_wrf,
            "source_forcing": forcing.get("source_forcing"),
            "execution_plan": self._execution_plan(request, source_wrf),
            "quality_gates": ([
                {"id": "forcing_contract", "status": "pass"},
                {"id": "wrf_topology", "status": "review" if forcing.get("topology_status") == "research_topology" else "pass"},
            ] if is_forcing_only else [
                {"id": "surface_model", "status": "pass", "mode": request.surface_mode},
                {"id": "coordinate_and_vertical_datum", "status": "pass"},
                {"id": "forcing_contract", "status": "pass"},
                {"id": "resource_plan", "status": "review" if domain and domain["review_reasons"] else "pass"},
            ]),
            "warnings": warnings,
            "surface": surface,
        }

    @staticmethod
    def _auto_upstream_request(request: DownscaleValidationRequest) -> dict[str, Any]:
        """Build the ordinary 1 km WRF request for a one-click high-res task."""
        if request.wrf.boundary_source not in {"gfs", "ecmwf"}:
            raise ValueError("高分一键上游目前只支持 GFS 或 ECMWF 边界资料")
        # The normal parent task is deliberately canonical: ordinary-task
        # defaults or a historical 25 km/34 m draft must never change the
        # high-resolution upstream contract.
        domains = [
            {"id": "d01", "dx": 27000, "dy": 27000, "e_we": 100, "e_sn": 79, "parent_id": 0, "parent_grid_ratio": 1, "i_parent_start": 1, "j_parent_start": 1},
            {"id": "d02", "dx": 9000, "dy": 9000, "e_we": 79, "e_sn": 61, "parent_id": 1, "parent_grid_ratio": 3, "i_parent_start": 11, "j_parent_start": 11},
            {"id": "d03", "dx": 3000, "dy": 3000, "e_we": 64, "e_sn": 46, "parent_id": 2, "parent_grid_ratio": 3, "i_parent_start": 6, "j_parent_start": 6},
            {"id": "d04", "dx": 1000, "dy": 1000, "e_we": 55, "e_sn": 40, "parent_id": 3, "parent_grid_ratio": 3, "i_parent_start": 6, "j_parent_start": 6},
        ]
        # Anchor the selected geographic position to the innermost grid centre.
        nested = request.wrf.domains
        grid_x, grid_y = (nested[-1].e_we + 1) / 2, (nested[-1].e_sn + 1) / 2
        for domain in reversed(nested[1:]):
            grid_x = domain.parent_start_i + (grid_x - 1) / domain.parent_grid_ratio
            grid_y = domain.parent_start_j + (grid_y - 1) / domain.parent_grid_ratio
        physics = request.wrf.physics
        cu_by_domain = list(physics.cu_physics_by_domain[:4])
        if not cu_by_domain:
            cu_by_domain = [0] * len(domains)
        value = {
            "data_source": request.wrf.boundary_source,
            "start_time": as_utc(request.start_time).isoformat().replace("+00:00", "Z"),
            "end_time": as_utc(request.end_time).isoformat().replace("+00:00", "Z"),
            "center": {"lat": request.aoi.center_lat, "lon": request.aoi.center_lon, "grid_x": grid_x, "grid_y": grid_y},
            "forecast_interval_hours": 3 if request.wrf.boundary_source == "ecmwf" else 1,
            "domains": domains,
            "physics": {
                "preset": "高分自动上游",
                "mp_physics": physics.mp_physics,
                "cu_physics": cu_by_domain[0] if cu_by_domain else 0,
                "cu_physics_by_domain": cu_by_domain,
                "ra_lw_physics": physics.ra_lw_physics,
                "ra_sw_physics": physics.ra_sw_physics,
                "bl_pbl_physics": physics.bl_pbl_physics,
                "sf_sfclay_physics": physics.sf_sfclay_physics,
                "sf_surface_physics": physics.sf_surface_physics,
                "sf_urban_physics": 0,
                "num_soil_layers": 4,
                "num_land_cat": 21,
                "radt": max(1, min(60, int(physics.radt_minutes))),
            },
            "assimilation_scheme": "off",
        }
        from schemas import WrfTaskCreate
        return WrfTaskCreate.model_validate(value).model_dump(mode="json")

    def create(self, payload: DownscaleTaskCreate, owner_sub: str, idempotency_key: str | None, owner_role: int = 1) -> dict[str, Any]:
        with self._submission_lock:
            return self._create(payload, owner_sub, idempotency_key, owner_role)

    def _create(self, payload: DownscaleTaskCreate, owner_sub: str, idempotency_key: str | None, owner_role: int = 1) -> dict[str, Any]:
        request = DownscaleValidationRequest.model_validate(payload.model_dump(exclude={"confirmation_token"}))
        parameterization = self._parameterization(request)
        source_wrf = self._source_wrf(request, owner_sub, owner_role)
        canonical = json.dumps(self._canonical(request), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        existing_id = None
        if idempotency_key:
            existing_id = self.store.submission(owner_sub, idempotency_key, canonical)
            if existing_id and (existing := self.store.get(existing_id)):
                return existing
        digest = hashlib.sha256(payload.confirmation_token.encode()).hexdigest()
        if not existing_id and not self.store.consume_confirmation(digest, owner_sub, canonical, utc_now()):
            raise ValueError("确认令牌无效、已使用、已过期，或与当前请求不匹配")
        is_forcing_only = request.execution_scope == "forcing_only"
        terrain: dict[str, Any] | None = None
        surface: dict[str, Any] | None = None
        domain: dict[str, Any] | None = None
        if not is_forcing_only:
            terrain, surface = self._surface_inputs(request)
            domain = self._derive_domain(request, terrain)
        forcing = self._forcing_check(request, domain, terrain, owner_sub, owner_role)
        certification = self._certification(request)
        if forcing.get("topology_status") == "research_topology" and domain is not None:
            domain["review_reasons"].append("WRF 嵌套拓扑偏离认证候选结构")
            domain["queue_mode"] = "review"
        task_id = f"ds_{as_utc(request.start_time):%Y%m%dT%H%M%SZ}_{secrets.token_hex(4)}"
        if idempotency_key:
            task_id = self.store.reserve_submission(owner_sub, idempotency_key, self._canonical(request), task_id)
            if existing := self.store.get(task_id):
                return existing
        if source_wrf.get("auto_create") and self.normal_task_submit is not None:
            upstream_request = self._auto_upstream_request(request)
            upstream_request["highres_parent_task_id"] = task_id
            upstream = self.normal_task_submit(upstream_request, owner_sub, f"highres-upstream-{task_id}")
            source_wrf = {
                "task_id": upstream.get("id"),
                "status": upstream.get("status") or "queued",
                "final_dx_m": NORMAL_WRF_DX_M,
                "auto_created": True,
                "owner_role": owner_role,
            }
        runtime = {
            "workflow_id": task_id,
            "workflow_kind": "high_resolution_wrf_to_cfd",
            "execution_mode": "managed_remote_runner_required",
            "retention": {"native_online_days": self.settings.downscale_native_retention_days, "archive_required": True},
            "mesoscale_window": {"start_utc": as_utc(request.start_time).isoformat().replace("+00:00", "Z"), "end_utc": as_utc(request.end_time).isoformat().replace("+00:00", "Z"), "duration_hours": forcing_duration_hours(request.start_time, request.end_time), "cadence": "PT1H", "available_times": len(valid_times(request.start_time, request.end_time))},
            "execution_scope": request.execution_scope,
            "microscale_sampling": None if is_forcing_only else self._sampling_preview(request),
            "cfd_domain": domain,
            "forcing": forcing,
            "parameterization": parameterization,
            "source_forcing": forcing.get("source_forcing"),
            "surface": surface,
            "terrain": None if request.surface_mode != "reviewed_terrain" or terrain is None else {"id": terrain["id"], "version": terrain["version"], "checksum_sha256": terrain["checksum_sha256"], "vertical_datum": terrain["vertical_datum"], "asset_files": terrain.get("asset_files", [])},
            "configuration_hash": self._recipe_hash(request),
            "source_wrf": source_wrf,
            "execution_plan": self._execution_plan(request, source_wrf),
        }
        requires_review = bool(domain and domain["review_reasons"]) or forcing.get("topology_status") == "research_topology"
        task = self.store.create(
            task_id, owner_sub, self._canonical(request), runtime,
            status="pending_review" if requires_review else "queued",
            stage="resource_scientific_review" if requires_review else "waiting_upstream",
            certification=certification,
        )
        contract = stage_runner_contract(
            self.settings.run_dir / "downscale_jobs", task_id, task["request"], task["runtime"],
        )
        self.store.append_log(task_id, f"已生成 {contract['runner_template_id']} 受管运行合同，SHA-256={contract['sha256']}。")
        return self.store.update(task_id, runtime={**task["runtime"], "runner_contract": contract}) or task

    def templates(self) -> dict[str, Any]:
        return {
            "schema_version": "downscale-config/v3",
            "limits": {
                "automatic_aoi_longest_side_m": AUTO_QUEUE_MAX_AOI_SIDE_M,
                "rans_snapshot_count": {"min": 1, "max": 6}, "les_episode_count": {"min": 1, "max": 2},
                # Templates describe the backward-compatible default window;
                # a concrete task derives its own duration from start/end.
                "forcing_duration_hours": DEFAULT_FORCING_DURATION_HOURS, "forcing_time_count": FORCING_TIME_COUNT,
            },
            "defaults": {
                "wrf": DownscaleValidationRequest.model_fields["wrf"].default_factory().model_dump(mode="json"),
                "cfd": DownscaleValidationRequest.model_fields["cfd"].default_factory().model_dump(mode="json"),
            },
            "parameterization": {
                "schema_version": PROFILE_SCHEMA_VERSION,
                "default_profile_id": DEFAULT_PARAMETERIZATION_PROFILE_ID,
                "profiles": parameterization_profiles(),
            },
            "certification_profiles": self.store.certification_profiles(),
        }

    def get_visible(self, task_id: str, owner_sub: str, role: int) -> dict[str, Any]:
        task = self.store.get(task_id)
        if not task or (role < 3 and task.get("owner_sub") != owner_sub):
            raise KeyError(task_id)
        return task

    def list_visible(self, limit: int, owner_sub: str, role: int) -> list[dict[str, Any]]:
        return self.store.list(limit, None if role >= 3 else owner_sub)

    def cancel(self, task_id: str, owner_sub: str, role: int) -> dict[str, Any]:
        task = self.get_visible(task_id, owner_sub, role)
        if task["status"] not in {"queued", "waiting_upstream", "preparing_forcing", "pending_review"}:
            raise ValueError("运行中的高分任务只能由受管远端执行器取消")
        self.store.append_log(task_id, "任务在远端启动前已取消。")
        upstream = task.get("runtime", {}).get("source_wrf") or {}
        if upstream.get("auto_created") and upstream.get("task_id") and self.normal_task_cancel is not None:
            try:
                self.normal_task_cancel(str(upstream["task_id"]))
                self.store.append_log(task_id, f"已同步取消自动创建的普通 WRF 上游 {upstream['task_id']}。")
            except Exception as exc:
                self.store.append_log(task_id, f"普通 WRF 上游取消未完成：{exc}")
        return self.store.update(task_id, status="cancelled", stage="cancelled", progress=0, publication={"status": "blocked"}) or task

    def retry(self, task_id: str, owner_sub: str, role: int) -> dict[str, Any]:
        with self._submission_lock:
            return self._retry(task_id, owner_sub, role)

    def _retry(self, task_id: str, owner_sub: str, role: int) -> dict[str, Any]:
        task = self.get_visible(task_id, owner_sub, role)
        if task["status"] not in {"failed", "cancelled", "partial_success"}:
            raise ValueError("当前高分任务不可重试")
        runtime = dict(task.get("runtime") or {})
        source = runtime.get("source_wrf") or {}
        if source.get("auto_created") and self.normal_task_lookup:
            normal = self.normal_task_lookup(source["task_id"], owner_sub, role)
            if not normal or normal.get("status") in {"failed", "cancelled", "waiting_restart"}:
                if self.normal_task_submit is None:
                    raise ValueError("自动上游提交服务不可用")
                attempt = int(runtime.get("upstream_attempt") or 0) + 1
                request = DownscaleValidationRequest.model_validate(task["request"])
                upstream_request = self._auto_upstream_request(request)
                upstream_request["highres_parent_task_id"] = task_id
                upstream = self.normal_task_submit(upstream_request, owner_sub, f"highres-upstream-{task_id}-{attempt}")
                runtime["upstream_attempt"] = attempt
                runtime["source_wrf"] = {"task_id": upstream["id"], "status": upstream["status"], "auto_created": True, "owner_role": role, "final_dx_m": NORMAL_WRF_DX_M}
        self.store.append_log(task_id, "已创建新的受管运行尝试；旧产物不会被复用为正式结果。")
        return self.store.update(
            task_id, runtime=runtime, status="queued", stage="waiting_upstream", progress=0, error=None,
            quality={"status": "pending", "gates": []}, publication={"status": "blocked"},
        ) or task

    def logs(self, task_id: str, owner_sub: str, role: int, after: int, limit: int) -> dict[str, Any]:
        self.get_visible(task_id, owner_sub, role)
        return self.store.logs(task_id, after, limit)

    def request_certification(self, task_id: str, owner_sub: str, role: int, payload: CertificationRequest) -> dict[str, Any]:
        task = self.get_visible(task_id, owner_sub, role)
        if task.get("quality", {}).get("status") != "complete":
            raise ValueError("仅完整通过质量门的任务可提交配置认证审核")
        certification = task.get("certification") or {}
        if certification.get("status") == "certified":
            raise ValueError("该任务已使用认证配置")
        request_id = f"cert_{secrets.token_hex(8)}"
        evidence = payload.model_dump(mode="json")
        record = self.store.create_certification_request(
            request_id, task_id, owner_sub, str(certification.get("recipe_hash") or task["runtime"].get("configuration_hash")), evidence,
        )
        self.store.append_log(task_id, "已提交科学配置认证审核；内部预览不会自动转为正式发布。")
        return record

    def review_certification(
        self, request_id: str, reviewer_sub: str, payload: CertificationReview
    ) -> dict[str, Any]:
        record = self.store.certification_request(request_id)
        if not record:
            raise KeyError(request_id)
        if record["status"] != "pending_review":
            raise ValueError("该认证申请已完成审核")
        review = {"reviewer_sub": reviewer_sub, "note": payload.note, "reviewed_at": utc_now()}
        if not payload.approved:
            self.store.update_certification_request(request_id, "rejected", review)
            return {"request_id": request_id, "status": "rejected", "review": review}
        profile_id = payload.profile_id or f"certified-{record['recipe_hash'][:12]}"
        task = self.store.get(record["task_id"])
        if not task:
            raise KeyError(record["task_id"])
        profile = self.store.register_certification_profile(
            profile_id, record["recipe_hash"], record["task_id"],
            {"recipe": self._recipe(DownscaleValidationRequest.model_validate(task["request"])), "review": review},
        )
        self.store.update_certification_request(request_id, "approved", {**review, "profile_id": profile_id})
        self.store.update(
            record["task_id"],
            certification={"status": "certified", "profile_id": profile_id, "recipe_hash": record["recipe_hash"], "certified_at": utc_now()},
        )
        self.store.append_log(record["task_id"], f"科学配置已冻结为认证版本 {profile_id}；后续精确匹配任务可正式发布。")
        return {"request_id": request_id, "status": "approved", "profile": profile, "review": review}

    def publish_bundle(self, task_id: str, bundle_root: Path) -> dict[str, Any]:
        """Internal runner hook; no browser endpoint may bypass this gate."""
        task = self.store.get(task_id)
        if not task:
            raise KeyError(task_id)
        manifest = validate_product_bundle(bundle_root, task_id, task["request"])
        request = task.get("request") or {}
        domains = ((request.get("wrf") or {}).get("domains") or [])
        final_dx = float((domains[-1] or {}).get("dx_m") or 0) if domains else 0.0
        is_new_forcing = request.get("upstream_mode") == "reuse_10m" or (
            request.get("upstream_mode") == "full_chain" and abs(final_dx - FORCING_DX_M) <= 0.02
        )
        if is_new_forcing:
            expected_configuration_hash = (task.get("runtime") or {}).get("configuration_hash")
            if manifest.get("configuration_hash") != expected_configuration_hash:
                raise ValueError("CFD 产品配置哈希与任务合同不一致")
        certified = task.get("certification", {}).get("status") == "certified"
        quality_gates = manifest.get("quality_gates") or []
        if certified and self.settings.catalog_publish_enabled and any(gate.get("status") != "pass" for gate in quality_gates):
            raise ValueError("正式发布要求所有质量门均为 pass；当前产品只能进入内部预览")
        target = atomically_publish_bundle(bundle_root, self.settings.output_dir / "runs", task_id, task["request"])
        publication: dict[str, Any] = {"status": "internal_preview", "mode": "backend_wrf_static"}
        product_url = f"/data/WRF/runs/{task_id}/downscale.product.json"
        if certified and self.settings.catalog_publish_enabled:
            compatibility_path = target / "scene.meta.json"
            if not compatibility_path.is_file():
                raise ValueError("正式统一发布要求受管运行器同时提供 scene.meta.json 兼容目录")
            compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
            if compatibility.get("quality", {}).get("status") != "complete" or compatibility.get("product_semantics") not in {"representative_steady_snapshot", "les_episode_statistics"}:
                raise ValueError("兼容目录必须声明 complete 质量门和正确的高分时间语义")
            publication = publish_workbench_result(task_id, task["owner_sub"], target, compatibility, self.settings.catalog_data_root)
            product_url = f"/data/WRF/workbench/{task_id}/downscale.product.json"
        elif certified:
            publication = {"status": "published", "mode": "backend_wrf_static", "catalog": "disabled"}
        result = {
            "downscale_product_url": product_url,
            "scene_3d_url": manifest.get("scene_3d_url") or (manifest.get("browser_preview") or {}).get("scene_manifest"),
            "browser_preview": manifest.get("browser_preview") or {},
            "browser_previews": manifest.get("browser_previews") or {},
            "standard_products": manifest.get("standard_products"),
            "surface_model": manifest.get("surface_model") or {},
            "catalog_publication": publication,
        }
        message = "产品包已原子发布到内部预览；配置尚未认证，不能进入统一正式目录。"
        if publication.get("status") in {"published", "success"}:
            message = "产品包通过 SHA-256、时间语义和质量门校验，已原子发布。"
        self.store.append_log(task_id, message)
        return self.store.update(
            task_id, status="succeeded", stage="done", progress=100, result=result,
            quality={"status": "complete", "gates": quality_gates}, publication=publication,
        ) or task
