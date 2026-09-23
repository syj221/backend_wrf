from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


UTC = timezone.utc
DEFAULT_FORCING_DURATION_HOURS = 48
MAX_FORCING_DURATION_HOURS = 168
# Kept as a compatibility constant for legacy contracts and callers. New
# contracts derive the count from start_time/end_time.
FORCING_DURATION_HOURS = DEFAULT_FORCING_DURATION_HOURS
FORCING_SPINUP_HOURS = 6
NORMAL_WRF_DX_M = 1_000.0
NOMINAL_FORCING_RESOLUTION_M = 10
FORCING_DX_M = 27_000.0 / (3**7)
LEGACY_FORCING_DX_M = 34.293556
FORCING_TOPOLOGY_ID = "normal-27km-to-wrf-les-10m/v1"
FORCING_SCHEMA_VERSION = "mesoscale-forcing/v2"
WRF_LES_PROFILE_ID = "wrf-3d-tke-les/v1"
FORCING_TIME_COUNT = DEFAULT_FORCING_DURATION_HOURS + 1
FORCING_INITIAL_TIME_STEP_SECONDS = 0.5
FORCING_EDGE_HALO_M = 500.0
MIN_PARENT_BUFFER_CELLS = 10
AUTO_QUEUE_MAX_AOI_SIDE_M = 2_000
AGL_LEVELS_M = [10, 40, 80, 120, 200]
REQUIRED_FORCING_VARIABLES = {
    "U", "V", "W", "T", "P", "PB", "PH", "PHB", "QVAPOR", "QKE", "HGT", "UST", "HFX", "PBLH",
}
AUTO_FLAT_SURFACE_VARIABLES = {"LU_INDEX", "ZNT"}
LEGACY_REQUIRED_FORCING_VARIABLES = REQUIRED_FORCING_VARIABLES - {"QKE", "HGT"}


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def forcing_duration_hours(start: datetime, end: datetime | None = None) -> int:
    """Return the inclusive hourly forcing window length.

    ``end`` is optional so old 48-hour contracts continue to validate. New
    tasks should provide it explicitly.
    """
    start = as_utc(start)
    end = as_utc(end) if end is not None else start + timedelta(hours=DEFAULT_FORCING_DURATION_HOURS)
    seconds = (end - start).total_seconds()
    if seconds <= 0 or seconds % 3600:
        raise ValueError("高分辨率任务起止时间必须为正数整小时")
    hours = int(seconds // 3600)
    if hours > MAX_FORCING_DURATION_HOURS:
        raise ValueError(f"高分辨率任务时间窗口不得超过 {MAX_FORCING_DURATION_HOURS} 小时")
    return hours


def valid_times(start: datetime, end: datetime | None = None) -> list[str]:
    start = as_utc(start)
    duration = forcing_duration_hours(start, end)
    return [
        (start + timedelta(hours=index)).isoformat().replace("+00:00", "Z")
        for index in range(duration + 1)
    ]


class DownscaleAoi(BaseModel):
    """A rectangle in the audited projected CRS, plus its WRF geographic anchor."""

    center_lat: float = Field(ge=-90, le=90)
    center_lon: float = Field(ge=-180, le=180)
    horizontal_crs: str = Field(min_length=3, max_length=80)
    # Geographic selection is the primary UI input. Projected coordinates are
    # retained when supplied by a reviewed asset/map service, but must never be
    # a mandatory manual-entry hurdle for a user drawing an AOI on the map.
    center_x_m: float | None = None
    center_y_m: float | None = None
    width_m: int = Field(gt=0)
    height_m: int = Field(gt=0)

    @property
    def longest_side_m(self) -> int:
        return max(self.width_m, self.height_m)


class WrfNestDomain(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    dx_m: float = Field(gt=0)
    dy_m: float = Field(gt=0)
    parent_grid_ratio: int = Field(ge=1, le=9)
    e_we: int = Field(ge=31, le=3001)
    e_sn: int = Field(ge=31, le=3001)
    parent_start_i: int = Field(ge=1)
    parent_start_j: int = Field(ge=1)


def default_wrf_domains() -> list[WrfNestDomain]:
    values = [27_000.0 / (3**index) for index in range(8)]
    # d01-d04 match the normal WRF defaults. d05-d08 are compact nested
    # domains around the default 500 m AOI, with at least ten parent cells of
    # buffer at every interface.
    shapes = [(100, 79), (79, 61), (64, 46), (55, 40), (37, 37), (43, 43), (64, 64), (127, 127)]
    starts = [(1, 1), (11, 11), (6, 6), (6, 6), (22, 15), (11, 11), (11, 11), (11, 11)]
    return [
        WrfNestDomain(
            name=f"d{index + 1:02d}", dx_m=value, dy_m=value,
            parent_grid_ratio=1 if index == 0 else 3,
            e_we=shapes[index][0], e_sn=shapes[index][1],
            parent_start_i=starts[index][0], parent_start_j=starts[index][1],
        )
        for index, value in enumerate(values)
    ]


class WrfPhysicsSpec(BaseModel):
    """Typed, auditable WRF choices; this is deliberately not raw namelist text."""

    profile_id: str = WRF_LES_PROFILE_ID
    mp_physics: int = Field(ge=0, le=99)
    ra_lw_physics: int = Field(ge=0, le=99)
    ra_sw_physics: int = Field(ge=0, le=99)
    bl_pbl_physics: int = Field(ge=0, le=99)
    sf_sfclay_physics: int = Field(ge=0, le=99)
    sf_surface_physics: int = Field(ge=0, le=99)
    cu_physics_by_domain: list[int] = Field(min_length=1, max_length=12)
    radt_minutes: int = Field(ge=0, le=120)


def default_wrf_physics() -> WrfPhysicsSpec:
    # A visible research candidate only. It is never treated as a certified regional recipe.
    return WrfPhysicsSpec(
        mp_physics=8, ra_lw_physics=4, ra_sw_physics=4, bl_pbl_physics=2,
        sf_sfclay_physics=2, sf_surface_physics=2,
        cu_physics_by_domain=[3, 0, 0, 0, 0, 0, 0, 0], radt_minutes=1,
    )


class WrfForcingSpec(BaseModel):
    domains: list[WrfNestDomain] = Field(default_factory=default_wrf_domains, min_length=2, max_length=12)
    physics: WrfPhysicsSpec = Field(default_factory=default_wrf_physics)
    e_vert: int = Field(default=88, ge=88, le=88)
    time_step_seconds: float = Field(default=FORCING_INITIAL_TIME_STEP_SECONDS, gt=0, le=300)
    spinup_hours: int = Field(default=FORCING_SPINUP_HOURS, ge=0, le=48)
    boundary_source: Literal["gfs", "ecmwf", "era5"] = "gfs"
    ndown_template_id: str = "ndown-normal-27km-to-10m/v1"
    output_variables: list[str] = Field(default_factory=lambda: sorted(REQUIRED_FORCING_VARIABLES | AUTO_FLAT_SURFACE_VARIABLES))

    @model_validator(mode="after")
    def topology_and_output_contract(self):
        if self.e_vert != 88:
            raise ValueError("第一版约 10 m WRF-LES forcing 固定使用 88 个垂直层")
        if self.physics.profile_id != WRF_LES_PROFILE_ID:
            raise ValueError("第一版约 10 m WRF-LES forcing 必须使用 wrf-3d-tke-les/v1 物理配置")
        production = (
            len(self.domains) == 8
            and abs(self.domains[0].dx_m - 27_000.0) <= 0.1
            and abs(self.final_dx_m - FORCING_DX_M) <= 0.02
        )
        if production and abs(self.time_step_seconds - FORCING_INITIAL_TIME_STEP_SECONDS) > 1e-9:
            raise ValueError("约 10 m forcing 的受管初始时间步固定为 0.5 秒")
        if len(self.physics.cu_physics_by_domain) != len(self.domains):
            raise ValueError("cu_physics_by_domain 必须为每个 WRF 域提供一个值")
        variables = {item.strip().upper() for item in self.output_variables if item.strip()}
        missing_set = REQUIRED_FORCING_VARIABLES - variables
        # WRF builds expose the 3-D turbulence diagnostic as either QKE or
        # TKE depending on the enabled Registry packages.
        if "QKE" in missing_set and "TKE" in variables:
            missing_set.remove("QKE")
        missing = sorted(missing_set)
        if missing:
            raise ValueError(f"约 10 m forcing 缺少必需输出变量：{', '.join(missing)}")
        for index, domain in enumerate(self.domains):
            if abs(domain.dx_m - domain.dy_m) > 1e-6:
                raise ValueError(f"{domain.name} 的 dx 与 dy 必须一致")
            if index == 0:
                continue
            parent = self.domains[index - 1]
            ratio = parent.dx_m / domain.dx_m
            if abs(ratio - domain.parent_grid_ratio) > 0.01:
                raise ValueError(f"{domain.name} 的 parent_grid_ratio 与分辨率不一致")
        return self

    @property
    def final_dx_m(self) -> float:
        return self.domains[-1].dx_m

    def production_topology_ok(self) -> bool:
        if len(self.domains) != 8 or abs(self.domains[0].dx_m - 27_000) > 0.1:
            return False
        if abs(self.final_dx_m - FORCING_DX_M) > 0.02:
            return False
        return all(domain.parent_grid_ratio == 3 for domain in self.domains[1:])

    def production_coverage(self, aoi_width_m: float, aoi_height_m: float) -> dict:
        """Check the fixed topology against the requested AOI and halos."""
        if not self.production_topology_ok():
            return {"valid": False, "reason": "topology"}
        final = self.domains[-1]
        extent_x = (final.e_we - 1) * final.dx_m
        extent_y = (final.e_sn - 1) * final.dy_m
        required_x = float(aoi_width_m) + 2 * FORCING_EDGE_HALO_M
        required_y = float(aoi_height_m) + 2 * FORCING_EDGE_HALO_M
        interface_buffers = []
        # d01-d04 belong to the ordinary WRF upstream contract.  The forcing
        # route owns the four new interfaces beginning at d04 -> d05.
        for parent, child in zip(self.domains[3:], self.domains[4:]):
            parent_extent_x = (parent.e_we - 1) * parent.dx_m
            parent_extent_y = (parent.e_sn - 1) * parent.dy_m
            child_extent_x = (child.e_we - 1) * child.dx_m
            child_extent_y = (child.e_sn - 1) * child.dy_m
            left_x = (child.parent_start_i - 1) * parent.dx_m
            left_y = (child.parent_start_j - 1) * parent.dy_m
            right_x = parent_extent_x - left_x - child_extent_x
            right_y = parent_extent_y - left_y - child_extent_y
            interface_buffers.append({
                "parent": parent.name,
                "child": child.name,
                "min_parent_cells": min(left_x, right_x) / parent.dx_m,
                "min_parent_cells_y": min(left_y, right_y) / parent.dy_m,
                "valid": min(left_x, right_x) >= MIN_PARENT_BUFFER_CELLS * parent.dx_m and min(left_y, right_y) >= MIN_PARENT_BUFFER_CELLS * parent.dy_m,
            })
        valid = extent_x >= required_x and extent_y >= required_y and all(item["valid"] for item in interface_buffers)
        return {
            "valid": valid,
            "aoi_width_m": float(aoi_width_m),
            "aoi_height_m": float(aoi_height_m),
            "halo_each_side_m": FORCING_EDGE_HALO_M,
            "required_extent_m": {"x": required_x, "y": required_y},
            "final_extent_m": {"x": extent_x, "y": extent_y},
            "minimum_parent_buffer_cells": MIN_PARENT_BUFFER_CELLS,
            "interfaces": interface_buffers,
        }


class MeshBand(BaseModel):
    name: str
    max_cell_m: float = Field(gt=0, le=64)
    top_agl_m: float = Field(gt=0)
    horizontal_extension_m: float = Field(ge=0)


def default_mesh_bands() -> list[MeshBand]:
    return [
        MeshBand(name="publication_core", max_cell_m=1, top_agl_m=16, horizontal_extension_m=0),
        MeshBand(name="near_buffer", max_cell_m=2, top_agl_m=48, horizontal_extension_m=50),
        MeshBand(name="mid_buffer", max_cell_m=4, top_agl_m=112, horizontal_extension_m=150),
        MeshBand(name="outer_domain", max_cell_m=8, top_agl_m=192, horizontal_extension_m=0),
        MeshBand(name="upper_domain", max_cell_m=16, top_agl_m=384, horizontal_extension_m=0),
    ]


class CfdDomainSpec(BaseModel):
    buffer_m: float | None = Field(default=None, gt=0)
    z_top_m_agl: float | None = Field(default=None, gt=0)
    mesh_bands: list[MeshBand] = Field(default_factory=default_mesh_bands, min_length=2, max_length=10)
    transition_cells: int = Field(default=3, ge=3, le=12)
    surface_refinement_m: float = Field(default=1, gt=0, le=8)

    @model_validator(mode="after")
    def graded_mesh(self):
        previous = 0.0
        for band in self.mesh_bands:
            if previous and band.max_cell_m > previous * 2.01:
                raise ValueError("相邻网格层级不得粗化超过 2 倍")
            previous = band.max_cell_m
        return self


class TurbulenceBoundarySpec(BaseModel):
    source: Literal["wrf_tke", "diagnostic_closure"] = "diagnostic_closure"
    closure_template_id: str = "mo-abl-diagnostic/v2"
    boundary_template_id: str = "wrf-flux-aware-mixed-open/v2"


class RansSamplingSpec(BaseModel):
    selected_hours: list[int] = Field(default_factory=lambda: [0, 12, 24, 36, 48], min_length=1, max_length=6)
    selection_mode: Literal["system_recommended", "user_confirmed"] = "system_recommended"

    @model_validator(mode="after")
    def unique_valid_hours(self):
        if len(set(self.selected_hours)) != len(self.selected_hours) or any(hour < 0 or hour > MAX_FORCING_DURATION_HOURS for hour in self.selected_hours):
            raise ValueError(f"RANS 时次必须是 0–{MAX_FORCING_DURATION_HOURS} 的不重复整点")
        return self


class LesEpisodeSpec(BaseModel):
    forcing_hour: int = Field(ge=0, le=MAX_FORCING_DURATION_HOURS)
    spinup_seconds: int = Field(default=600, ge=0, le=86_400)
    statistics_window_seconds: int = Field(default=1_800, ge=60, le=86_400)
    output_interval_seconds: int = Field(default=60, ge=1, le=3_600)
    max_courant: float = Field(default=0.8, gt=0, le=1)
    inflow_method: Literal["synthetic_turbulence", "precursor_abl"] = "synthetic_turbulence"
    inflow_template_id: str = "digital-filter-inflow/v1"

    @model_validator(mode="after")
    def output_interval(self):
        if self.output_interval_seconds > self.statistics_window_seconds:
            raise ValueError("LES 输出间隔不得长于统计窗口")
        return self


class LesSamplingSpec(BaseModel):
    episodes: list[LesEpisodeSpec] = Field(default_factory=lambda: [LesEpisodeSpec(forcing_hour=12)], min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_forcing_hours(self):
        hours = [episode.forcing_hour for episode in self.episodes]
        if len(hours) != len(set(hours)):
            raise ValueError("LES 时段不能引用重复的 forcing 时次")
        return self


class CfdSpec(BaseModel):
    solver_family: Literal["rans", "les"] = "rans"
    thermal_mode: Literal["thermal", "neutral"] = "thermal"
    solver_template_id: str = "thermal-rans-graded/v2"
    domain: CfdDomainSpec = Field(default_factory=CfdDomainSpec)
    turbulence: TurbulenceBoundarySpec = Field(default_factory=TurbulenceBoundarySpec)
    publication_heights_agl_m: list[int] = Field(default_factory=lambda: list(AGL_LEVELS_M), min_length=1, max_length=20)
    rans: RansSamplingSpec | None = Field(default_factory=RansSamplingSpec)
    les: LesSamplingSpec | None = None

    @model_validator(mode="after")
    def solver_contract(self):
        if self.solver_family == "rans" and (self.rans is None or self.les is not None):
            raise ValueError("RANS 任务只能包含 rans 采样配置")
        if self.solver_family == "les" and (self.les is None or self.rans is not None):
            raise ValueError("LES 任务只能包含 les 时段配置")
        if any(height <= 0 for height in self.publication_heights_agl_m):
            raise ValueError("发布高度必须为正 AGL")
        return self


class DownscaleValidationRequest(BaseModel):
    execution_scope: Literal["wrf_cfd", "forcing_only"] = "forcing_only"
    upstream_mode: Literal["full_chain", "reuse_10m", "reuse_34m"] = "full_chain"
    start_time: datetime
    # Optional for backwards compatibility with v1 tasks; omitted values are
    # interpreted as the original 48-hour window.
    end_time: datetime | None = None
    terrain_asset_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    surface_mode: Literal["auto_flat", "reviewed_terrain"] | None = None
    # A normal WRF task may seed a downstream draft. It is lineage context only:
    # the high-resolution task still runs its own final NDOWN/real/wrf chain.
    source_wrf_task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    aoi: DownscaleAoi
    wrf: WrfForcingSpec = Field(default_factory=WrfForcingSpec)
    cfd: CfdSpec = Field(default_factory=CfdSpec)
    # A CFD task is a second, explicit hand-off after a certified mesoscale
    # forcing task.  The normal WRF task id remains required for lineage and
    # ACL checks, while this id identifies the immutable d08 forcing source.
    source_forcing_task_id: str | None = Field(default=None, pattern=r"^ds_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
    upstream_artifact_id: str | None = None
    upstream_content_sha256: str | None = None
    # Versioned business/scientific recipe selected by the UI. The final typed
    # WRF/CFD values remain in this request so exports and task contracts are
    # self-contained; the service compares them with the server-side profile.
    parameterization_profile_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,95}$")
    certification_profile_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")

    @model_validator(mode="after")
    def validate_contract(self):
        if self.surface_mode is None:
            # Old persisted tasks that carry a terrain id keep their original
            # meaning. New CFD requests without one use the managed flat model.
            self.surface_mode = "reviewed_terrain" if self.terrain_asset_id else "auto_flat"
        self.start_time = as_utc(self.start_time)
        if any((self.start_time.minute, self.start_time.second, self.start_time.microsecond)):
            raise ValueError("高分辨率任务起报时间必须为 UTC 整点")
        self.end_time = as_utc(self.end_time) if self.end_time is not None else self.start_time + timedelta(hours=DEFAULT_FORCING_DURATION_HOURS)
        if any((self.end_time.minute, self.end_time.second, self.end_time.microsecond)):
            raise ValueError("高分辨率任务结束时间必须为 UTC 整点")
        duration = forcing_duration_hours(self.start_time, self.end_time)
        if duration < 1:
            raise ValueError("高分辨率任务至少需要 1 小时 forcing")
        if self.cfd.solver_family == "rans" and self.cfd.rans is not None:
            if any(hour > duration for hour in self.cfd.rans.selected_hours):
                raise ValueError(f"RANS 时次必须落在 0–{duration} 小时 forcing 窗口内")
        if self.cfd.solver_family == "les" and self.cfd.les is not None:
            if any(episode.forcing_hour > duration for episode in self.cfd.les.episodes):
                raise ValueError(f"LES forcing 时次必须落在 0–{duration} 小时 forcing 窗口内")
        if self.execution_scope == "wrf_cfd" and self.surface_mode == "reviewed_terrain" and not self.terrain_asset_id:
            raise ValueError("真实地形 CFD 任务必须绑定审核地形资产")
        if self.execution_scope == "wrf_cfd" and self.surface_mode == "auto_flat" and self.terrain_asset_id:
            raise ValueError("自动平坦地表任务不能同时绑定真实地形资产")
        if self.execution_scope == "forcing_only" and self.upstream_mode != "full_chain":
            raise ValueError("仅生成约 10 m forcing 时必须执行完整受管链路，不能复用已有 forcing")
        if self.upstream_mode in {"reuse_10m", "reuse_34m"} and not self.source_wrf_task_id:
            raise ValueError("复用 forcing 的高分辨率任务必须绑定普通 WRF 上游任务")
        if self.upstream_mode == "reuse_10m":
            if self.execution_scope != "wrf_cfd":
                raise ValueError("约 10 m forcing → CFD 交接只能用于 WRF–CFD 任务")
            if not self.source_forcing_task_id:
                raise ValueError("约 10 m CFD 任务必须绑定一个已完成的 forcing 任务")
            if self.upstream_artifact_id or self.upstream_content_sha256:
                raise ValueError("约 10 m CFD 任务不接受客户端指定的旧 forcing artifact")
        elif self.source_forcing_task_id:
            raise ValueError("source_forcing_task_id 只能用于约 10 m forcing → CFD 交接")
        if self.cfd.turbulence.source == "wrf_tke" and not ({"TKE", "QKE"} & {value.upper() for value in self.wrf.output_variables}):
            raise ValueError("选择 WRF TKE 边界时，forcing 输出必须包含 TKE 或 QKE")
        if self.upstream_mode == "reuse_34m":
            if not self.upstream_artifact_id or not self.upstream_content_sha256:
                raise ValueError("复用 34 m forcing 必须提供 artifact_id 与内容哈希")
        elif self.upstream_artifact_id or self.upstream_content_sha256:
            raise ValueError("完整链路不接受客户端指定的 34 m forcing")
        return self


class DownscaleTaskCreate(DownscaleValidationRequest):
    confirmation_token: str = Field(min_length=32, max_length=4096)

    @model_validator(mode="after")
    def reject_legacy_production_mode(self):
        # Keep ``reuse_34m`` readable for historical task records through the
        # base request model, but never allow it to create a new production
        # task. New CFD inputs must use the certified d08 forcing contract.
        if self.upstream_mode == "reuse_34m":
            raise ValueError("reuse_34m 仅保留历史任务查看，不能创建新的生产 CFD 任务")
        return self


class TerrainAssetFile(BaseModel):
    file_uuid: str = Field(min_length=1, max_length=128)
    kind: Literal["dtm", "dsm", "buildings", "stl", "land_cover", "surface_properties"]
    file_name: str | None = Field(default=None, max_length=512)
    size_bytes: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class TerrainAsset(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    label: str
    version: str
    status: Literal["approved", "pending_review", "unverified", "blocked", "rejected", "smoke"]
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    horizontal_crs: str = Field(min_length=3, max_length=80)
    vertical_datum: str = Field(min_length=1, max_length=80)
    coverage_width_m: float = Field(gt=0)
    coverage_height_m: float = Field(gt=0)
    # Required for automatic map-based suitability detection on newly reviewed
    # assets. Legacy records can remain visible but are marked unlocated.
    coverage_center_lat: float | None = Field(default=None, ge=-90, le=90)
    coverage_center_lon: float | None = Field(default=None, ge=-180, le=180)
    hchar_m: float = Field(ge=0)
    control_point_error_m: float = Field(ge=0)
    source_file_uuids: list[str] = Field(default_factory=list)
    asset_kinds: list[Literal["dtm", "dsm", "buildings", "stl", "land_cover", "surface_properties"]] = Field(default_factory=list)
    asset_files: list["TerrainAssetFile"] = Field(default_factory=list)
    source: str = ""

    @model_validator(mode="after")
    def approved_asset_contract(self):
        if self.status == "approved":
            required = {"dtm", "dsm", "buildings", "stl", "land_cover", "surface_properties"}
            missing = sorted(required - set(self.asset_kinds))
            if missing:
                raise ValueError(f"审核地形资产缺少：{', '.join(missing)}")
            mapped = {item.kind for item in self.asset_files}
            if mapped and required - mapped:
                raise ValueError(f"审核地形资产文件映射缺少：{', '.join(sorted(required - mapped))}")
            if not self.source_file_uuids and not self.asset_files:
                raise ValueError("审核地形资产必须关联 backend_upload 归档文件")
        return self


class TerrainAssetSubmission(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    label: str
    version: str
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    horizontal_crs: str = Field(min_length=3, max_length=80)
    vertical_datum: str = Field(min_length=1, max_length=80)
    coverage_width_m: float = Field(gt=0)
    coverage_height_m: float = Field(gt=0)
    coverage_center_lat: float | None = Field(default=None, ge=-90, le=90)
    coverage_center_lon: float | None = Field(default=None, ge=-180, le=180)
    hchar_m: float = Field(ge=0)
    control_point_error_m: float = Field(ge=0)
    source_file_uuids: list[str] = Field(min_length=1)
    asset_kinds: list[Literal["dtm", "dsm", "buildings", "stl", "land_cover", "surface_properties"]] = Field(min_length=1)
    asset_files: list[TerrainAssetFile] = Field(default_factory=list)
    source: str = "backend_upload"

    @model_validator(mode="after")
    def validate_file_mapping(self):
        if self.asset_files:
            required = {"dtm", "dsm", "buildings", "stl", "land_cover", "surface_properties"}
            kinds = [item.kind for item in self.asset_files]
            if set(kinds) != required or len(kinds) != len(required):
                raise ValueError("真实地形必须为六类资产各映射一个归档文件")
            if {item.file_uuid for item in self.asset_files} != set(self.source_file_uuids):
                raise ValueError("asset_files 与 source_file_uuids 必须一致")
        return self


class TerrainAssetReview(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=2_000)


class TerrainAssetResolveRequest(BaseModel):
    """Map-driven terrain suitability lookup before a CFD task is submitted."""

    aoi: DownscaleAoi


class CertificationRequest(BaseModel):
    evidence: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=4_000)


class CertificationReview(BaseModel):
    approved: bool
    profile_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    note: str = Field(default="", max_length=4_000)
