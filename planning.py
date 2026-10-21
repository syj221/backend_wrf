"""Side-effect-free, evidence-labelled assessment of an incomplete WRF request."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from domain_preview import AreaOfInterest, DomainPreviewRequest, preview_domains
from schemas import Center, DomainConfig, SpinupConfig
from wrf_knowledge import KnowledgeStore
from guidance import recommendations


class EvidenceQuery(BaseModel):
    target: Literal["precipitation", "wind", "thermodynamics_pbl", "solar_radiation",
                    "tropical_cyclone", "snow_ice", "urban_climate", "land_surface", "hydrology"]
    region: str = Field(min_length=1, max_length=200)
    season: str = Field(default="", max_length=100)


class PlanningAssessRequest(BaseModel):
    configuration_profile: Literal["wrf-ordinary-ui-1","wrf-ordinary-ui-2"] | None = None
    goal: str | None = Field(default=None, max_length=500)
    aoi: AreaOfInterest | None = None
    center: Center | None = None
    center_confirmed: bool = False
    start_time: datetime | None = None
    end_time: datetime | None = None
    data_source: Literal["gfs", "ecmwf"] | None = None
    forecast_interval_hours: Literal[1, 3, 6] | None = None
    history_interval_minutes: Literal[10, 30, 60, 180, 360] | None = None
    spinup: SpinupConfig = Field(default_factory=SpinupConfig)
    vertical_levels: int = Field(default=88, ge=10, le=500, strict=True)
    resolution_m: int | None = Field(default=None, ge=1000, le=50000)
    duration_hours: int | None = Field(default=None, ge=1, le=720)
    domain_count: int = Field(default=1, ge=1, le=4)
    nesting_ratio: Literal[3, 5] = 3
    domains: list[DomainConfig] | None = None
    physics_preset: str | None = None
    evidence_query: EvidenceQuery | None = None
    physics: dict[str, Any] | None = None
    assimilation_scheme: Literal["off", "fdda_weak", "fdda_standard", "fdda_strong"] | None = None
    projection: dict[str, Any] = Field(default_factory=dict)
    namelist: dict[str, Any] | None = None
    time_step_seconds: int | None = Field(default=None, ge=1)


def assess(payload: PlanningAssessRequest, health: dict[str, Any], pool=None) -> dict[str, Any]:
    missing: list[str] = []
    if payload.configuration_profile == "wrf-ordinary-ui-2":
        if not payload.center:
            missing.append("center")
    elif not payload.center or not payload.center_confirmed:
        missing.append("center_confirmation")
    if not payload.start_time or not payload.end_time:
        missing.append("time_window")
    if not payload.resolution_m and not payload.domains:
        missing.append("resolution")
    if not payload.data_source:
        missing.append("data_source")
    if not payload.physics_preset and not payload.physics:
        missing.append("physics")
    questions = {
        "goal": "主要关注降水、风、温度还是其他现象？",
        "center_confirmation": "请在地图确认关注区域和计算域中心。",
        "center": "请提供计算域中心坐标，或在地图绘制计算范围。",
        "time_window": "请确认具体起止日期与时间（同时显示 UTC/北京时间）。",
        "resolution": "希望最细网格距和覆盖范围是多少？",
        "data_source": "边界资料选择 GFS 还是 ECMWF？",
        "physics": "默认使用通用物理设置；是否需要调整具体参数？",
    }
    candidates: list[dict[str, Any]] = []
    if (payload.center or payload.aoi) and (payload.domains or payload.resolution_m):
        try:
            preview = preview_domains(DomainPreviewRequest(
                center=payload.center, aoi=payload.aoi, domains=payload.domains,
                resolution_m=payload.resolution_m or 27000,
                domain_count=payload.domain_count, nesting_ratio=payload.nesting_ratio,
                projection=payload.projection,
                configuration_profile=payload.configuration_profile,
            ))
            cells = sum(item["grid"][0] * item["grid"][1] for item in preview["boundaries"])
            candidates.append({
                "id": "requested_geometry", "label": "按当前空间要求",
                "center": preview["center"], "domains": preview["domains"],
                "center_confirmed": bool(payload.center_confirmed and preview["center_confirmed"]),
                "aoi_covered": preview["aoi_covered"], "grid_cells_per_output": cells,
                "finest_aoi_covered": preview["finest_aoi_covered"],
                "finest_resolution_m": min(item["dx"] for item in preview["domains"]),
                "warnings": preview["warnings"],
            })
            if payload.aoi and payload.resolution_m and not payload.domains:
                for multiplier, identifier, label in (
                    (3, "coarser_comparison", "较粗网格对照"),
                    (1 / 3, "finer_comparison", "较细网格对照"),
                ):
                    resolution = float(payload.resolution_m) * multiplier
                    if not resolution.is_integer() or not 1000 <= resolution <= 50000:
                        continue
                    try:
                        alternative = preview_domains(DomainPreviewRequest(
                            center=payload.center, aoi=payload.aoi, resolution_m=int(resolution),
                            domain_count=payload.domain_count, nesting_ratio=payload.nesting_ratio,
                            projection=payload.projection,
                            configuration_profile=payload.configuration_profile,
                        ))
                    except ValueError:
                        continue
                    if alternative["aoi_covered"] is False:
                        continue
                    candidates.append({
                        "id": identifier, "label": label,
                        "center": alternative["center"], "domains": alternative["domains"],
                        "center_confirmed": bool(payload.center_confirmed and alternative["center_confirmed"]),
                        "aoi_covered": alternative["aoi_covered"],
                        "grid_cells_per_output": sum(
                            item["grid"][0] * item["grid"][1] for item in alternative["boundaries"]),
                        "finest_resolution_m": int(resolution),
                        "comparison_only": True, "selection_required": True,
                        "warnings": ["仅供比较，不自动替换用户要求；较细网格不保证更高预报准确率。"],
                    })
        except ValueError as exc:
            candidates.append({"id": "requested_geometry", "label": "当前空间要求需调整",
                               "valid": False, "warnings": [str(exc)]})
    literature: dict[str, Any] = {"status": "not_requested", "candidates": []}
    if payload.evidence_query:
        finest = min((domain.dx for domain in (payload.domains or [])), default=payload.resolution_m or 0)
        if finest <= 0:
            literature = {"status": "needs_resolution", "candidates": [],
                          "reason": "缺少网格距，不能检索可比文献个例。"}
        else:
            try:
                query = payload.evidence_query
                matched = KnowledgeStore().recommend(
                    target=query.target, region=query.region, season=query.season,
                    dx_km=finest / 1000, limit=3,
                )
                literature = {
                    "status": matched["status"], "kb_version": matched.get("kb_version"),
                    "source_sha256": matched.get("source_sha256"),
                    "candidates": [{
                        "paper_id": str(item.get("paper_id") or "")[:80],
                        "title": str(item.get("title") or "")[:200],
                        "source_url": item.get("source_url") if str(item.get("source_url") or "").startswith("https://") else None,
                        "paper_wrf_version": str(item.get("paper_wrf_version") or "")[:80],
                        "matched": {
                            "region": str((item.get("matched") or {}).get("region") or "")[:120],
                            "target": query.target,
                            "dx_km": (item.get("matched") or {}).get("dx_km"),
                            "season": str((item.get("matched") or {}).get("season") or "")[:100],
                        },
                        "verification_status": (item.get("provenance") or {}).get("verification_status", "unverified"),
                        "source_file": str((item.get("provenance") or {}).get("file") or "").replace("\\", "/").rsplit("/", 1)[-1][:160],
                        "source_row": (item.get("provenance") or {}).get("row"),
                        "runnable": False,
                        "applicability": "仅按目标、区域和网格距检索；尚未经原文核验或当前版本运行验证。",
                    } for item in matched.get("candidates", [])[:3]],
                    "limitations": ["不跨论文直接比较性能指标", "文献候选不得直接作为可运行物理参数"],
                }
            except FileNotFoundError:
                literature = {"status": "knowledge_unavailable", "candidates": [],
                              "reason": "离线 WRF 知识库尚未导入；不生成虚构文献候选。"}
    request = payload.model_dump(mode="json", exclude_none=True)
    return {
        **recommendations(request, health, pool),
        "guidance": {"schema": "wrf-guidance-1", "items": [], "retired": True},
        "capability_version": "wrf-ordinary-3",
        "assessed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "missing_fields": missing,
        "questions": [questions[key] for key in missing[:3]],
        "candidates": candidates[:3],
        "data_coverage": {"gfs": "全球名义覆盖；目标时次是否已下载未知",
                          "ecmwf": "全球名义覆盖；目标时次是否已下载未知"},
        "hpc": {"status": str(health.get("status") or "unknown"),
                "sampled_at": health.get("sampled_at")},
        "resource_estimate": {"runtime": None, "memory": None,
                              "reason": "尚无经校准的机器和配置基准，不提供精确耗时或内存承诺。"},
        "evidence": [{"source": "/api/wrf/options and current service policy",
                      "status": "verified_code", "scope": "输入范围与固定策略"},
                     {"source": "cached_hpc_health", "status": "observed" if health.get("sampled_at") else "unknown",
                      "sampled_at": health.get("sampled_at")}],
        "literature_evidence": literature,
        "limitations": ["候选方案只说明几何与规模，不代表预报准确率；正式创建前仍须服务端 validate。"],
    }
