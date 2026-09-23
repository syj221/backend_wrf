from __future__ import annotations

from copy import deepcopy
from typing import Any

from .schemas import CfdSpec, WrfForcingSpec


PROFILE_SCHEMA_VERSION = "wrf-cfd-parameterization/v1"
DEFAULT_PARAMETERIZATION_PROFILE_ID = "standard-rans-thermal/v1"


def _profile(
    profile_id: str,
    label: str,
    description: str,
    use_cases: list[str],
    certification_status: str,
    release_mode: str,
    cfd_patch: dict[str, Any],
    *,
    recommended: bool = False,
    fixed_notes: list[str] | None = None,
    resource_estimate: dict[str, Any] | None = None,
    output_type: str = "",
    manual_review_required: bool = False,
    formal_release_allowed: bool = False,
    editable_parameters: list[str] | None = None,
) -> dict[str, Any]:
    wrf = WrfForcingSpec().model_dump(mode="json")
    cfd = CfdSpec().model_dump(mode="json")
    for key, value in cfd_patch.items():
        if isinstance(value, dict) and isinstance(cfd.get(key), dict):
            cfd[key] = {**cfd[key], **deepcopy(value)}
        else:
            cfd[key] = deepcopy(value)
    return {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "id": profile_id,
        "version": profile_id.rsplit("/", 1)[-1],
        "label": label,
        "description": description,
        "use_cases": use_cases,
        "recommended": recommended,
        "certification_status": certification_status,
        "release_mode": release_mode,
        "resource_estimate": resource_estimate or {},
        "output_type": output_type,
        "manual_review_required": manual_review_required,
        "formal_release_allowed": formal_release_allowed,
        "wrf": wrf,
        "cfd": cfd,
        "fixed_notes": fixed_notes or [],
        "editable_groups": ["aoi", "surface", "publication_heights", "sampling", "domain"],
        "editable_parameters": editable_parameters or [
            "aoi",
            "surface_mode",
            "terrain_asset_id",
            "cfd.publication_heights_agl_m",
            "cfd.rans.selected_hours",
            "cfd.les.episodes",
            "cfd.domain.buffer_m",
            "cfd.domain.z_top_m_agl",
            "cfd.domain.mesh_bands",
            "cfd.turbulence",
        ],
        "fixed_parameters": {
            "wrf.topology": "normal-27km-to-wrf-les-10m/v1 · d01-d08 · 3:1",
            "wrf.final_horizontal_spacing_m": 12.345679,
            "wrf.d05_d08.physics": {"bl_pbl_physics": 0, "diff_opt": 2, "km_opt": 2, "e_vert": 88, "cu_physics": 0},
            "forcing.valid_time_count": "由任务起止时间按逐小时生成",
            "forcing.required_turbulence": "WRF TKE/QKE or server-controlled diagnostic closure",
            "storage.execution": "managed data-volume alias and isolated task directory",
        },
    }


def _profiles() -> dict[str, dict[str, Any]]:
    standard = _profile(
        DEFAULT_PARAMETERIZATION_PROFILE_ID,
        "标准业务 RANS",
        "适合常规风场、温度和城市气象业务的稳态代表场。",
        ["常规风场", "城市气象", "业务预报"],
        "research",
        "internal_preview",
        {
            "solver_family": "rans",
            "thermal_mode": "thermal",
            "solver_template_id": "thermal-rans-graded/v2",
            "turbulence": {"source": "diagnostic_closure", "closure_template_id": "mo-abl-diagnostic/v2", "boundary_template_id": "wrf-flux-aware-mixed-open/v2"},
            "publication_heights_agl_m": [10, 40, 80, 120, 200],
            "rans": {"selected_hours": [0, 12, 24, 36, 48], "selection_mode": "system_recommended"},
            "les": None,
        },
        recommended=True,
        fixed_notes=["WRF-LES d05-d08 拓扑和物理边界由系统固定", "结果通过认证后才可正式发布"],
        resource_estimate={
            "reference_aoi_m": [500, 500],
            "end_to_end_runtime_hours": [12, 48],
            "peak_memory_gib": [16, 64],
            "disk_gib": [100, 240],
            "basis": "包含 d04→d08 forcing、5 个 RANS 快照和产品发布；实际值由 AOI、地形和队列负载复核",
        },
        output_type="1 m 核心采样的 RANS 独立稳态快照集",
        manual_review_required=False,
        formal_release_allowed=False,
    )
    neutral = _profile(
        "neutral-rans/v1",
        "中性大气 RANS",
        "适合关注地形、建筑物和机械湍流影响的稳态代表场。",
        ["机械湍流", "复杂地形", "建筑绕流"],
        "research",
        "internal_preview",
        {
            "solver_family": "rans",
            "thermal_mode": "neutral",
            "solver_template_id": "neutral-rans-graded/v1",
            "turbulence": {"source": "diagnostic_closure", "closure_template_id": "neutral-abl-diagnostic/v1", "boundary_template_id": "wrf-flux-aware-mixed-open/v2"},
            "publication_heights_agl_m": [10, 40, 80, 120, 200],
            "rans": {"selected_hours": [0, 12, 24, 36, 48], "selection_mode": "system_recommended"},
            "les": None,
        },
        fixed_notes=["中性模式需要单独完成科学验证", "当前默认只能作为内部研究预览"],
        resource_estimate={
            "reference_aoi_m": [500, 500],
            "end_to_end_runtime_hours": [12, 48],
            "peak_memory_gib": [16, 64],
            "disk_gib": [100, 240],
            "basis": "与标准业务 RANS 使用相同网格和代表时次；地形、建筑和粗糙度审核可能增加等待时间",
        },
        output_type="1 m 核心采样的中性 RANS 独立稳态快照集",
        manual_review_required=True,
        formal_release_allowed=False,
    )
    les = _profile(
        "research-les-episode/v1",
        "LES 研究方案",
        "适合研究局地非定常湍流和瞬态结构的有限统计时段。",
        ["瞬态湍流", "局地研究", "非定常结构"],
        "research",
        "internal_preview",
        {
            "solver_family": "les",
            "thermal_mode": "thermal",
            "solver_template_id": "thermal-les-graded/v1",
            "turbulence": {"source": "wrf_tke", "closure_template_id": "mo-abl-diagnostic/v2", "boundary_template_id": "wrf-flux-aware-mixed-open/v2"},
            "publication_heights_agl_m": [10, 40, 80, 120, 200],
            "rans": None,
            "les": {"episodes": [{"forcing_hour": 12, "spinup_seconds": 600, "statistics_window_seconds": 1800, "output_interval_seconds": 60, "max_courant": 0.8, "inflow_method": "synthetic_turbulence", "inflow_template_id": "digital-filter-inflow/v1"}]},
        },
        fixed_notes=["LES 结果是有限统计窗口，不是连续 1 m 预报", "最多配置两个独立 LES 时段"],
        resource_estimate={
            "reference_aoi_m": [500, 500],
            "end_to_end_runtime_hours": [24, 72],
            "peak_memory_gib": [32, 96],
            "disk_gib": [160, 360],
            "basis": "包含 d04→d08 forcing 和 1 个 LES 统计时段；增加第二个时段会增加求解时间和产品体量",
        },
        output_type="1 m 核心采样的有限 LES 统计时段产品",
        manual_review_required=True,
        formal_release_allowed=False,
    )
    return {item["id"]: item for item in (standard, neutral, les)}


PARAMETERIZATION_PROFILES = _profiles()


def parameterization_profiles() -> list[dict[str, Any]]:
    return deepcopy(list(PARAMETERIZATION_PROFILES.values()))


def parameterization_profile(profile_id: str | None) -> dict[str, Any]:
    selected = profile_id or DEFAULT_PARAMETERIZATION_PROFILE_ID
    try:
        return deepcopy(PARAMETERIZATION_PROFILES[selected])
    except KeyError as exc:
        raise ValueError(f"未知高分参数化方案：{selected}") from exc


def profile_recipe(profile: dict[str, Any]) -> dict[str, Any]:
    return {"wrf": deepcopy(profile["wrf"]), "cfd": deepcopy(profile["cfd"])}
