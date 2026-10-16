from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, SecretStr, model_validator


FIXED_RUNTIME_PROFILE = "cpu"
FIXED_SPINUP_HOURS = 6
DEFAULT_VERTICAL_LEVELS = 88
LEGACY_CONFIGURATION_PROFILE = "wrf-ordinary-ui-1"
CURRENT_CONFIGURATION_PROFILE = "wrf-ordinary-ui-2"
FIXED_FORECAST_FOCUS = "general"
PBL_SURFACE_COMPATIBILITY = {1: {1}, 2: {2}, 5: {5}, 6: {5}, 7: {7}}


def normalize_task_request(request: dict) -> dict:
    """Normalize CPU execution without replacing explicit configuration choices."""
    normalized = dict(request)
    source = str(normalized.get("data_source") or "gfs").strip().lower()
    normalized["data_source"] = "ecmwf" if source == "ec" else source
    normalized["runtime_profile"] = FIXED_RUNTIME_PROFILE
    normalized["spinup"] = {"mode": "custom", "hours": configured_spinup_hours(normalized)}
    normalized["vertical_levels"] = configured_vertical_levels(normalized)
    normalized["forecast_focus"] = FIXED_FORECAST_FOCUS
    return normalized


def configured_spinup_hours(request: dict) -> int:
    if request.get("configuration_profile") == LEGACY_CONFIGURATION_PROFILE:
        return FIXED_SPINUP_HOURS
    value = request.get("spinup") or {}
    if not isinstance(value, dict) or value.get("mode", "custom") != "custom":
        raise ValueError("spin-up 须使用 custom 模式和非负整数 hours")
    hours = value.get("hours", FIXED_SPINUP_HOURS)
    if type(hours) is not int or hours < 0:
        raise ValueError("spin-up 小时数须为非负整数")
    return hours


def configured_vertical_levels(request: dict) -> int:
    if request.get("configuration_profile") == LEGACY_CONFIGURATION_PROFILE:
        return DEFAULT_VERTICAL_LEVELS
    value = request.get("vertical_levels", DEFAULT_VERTICAL_LEVELS)
    if type(value) is not int or not 10 <= value <= 500:
        raise ValueError("垂直层数须为 10–500 的整数")
    return value


class SpinupConfig(BaseModel):
    mode: Literal["custom"] = "custom"
    hours: int = Field(default=FIXED_SPINUP_HOURS, ge=0, strict=True)


class Center(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    grid_x: float | None = Field(default=None, ge=1)
    grid_y: float | None = Field(default=None, ge=1)


class DomainConfig(BaseModel):
    id: str
    dx: int = Field(gt=0)
    dy: int = Field(gt=0)
    e_we: int = Field(ge=10, le=500)
    e_sn: int = Field(ge=10, le=500)
    parent_id: int = Field(ge=0)
    parent_grid_ratio: int = Field(default=1)
    i_parent_start: int = Field(default=1, ge=1)
    j_parent_start: int = Field(default=1, ge=1)


def validate_domain_tree(domains: list[DomainConfig], *, legacy=False) -> None:
    """Validate a topologically ordered ARW tree in each parent's grid space."""
    if not 1 <= len(domains) <= 4:
        raise ValueError("计算域总数必须为 1–4")
    siblings: dict[int, list[tuple[DomainConfig, tuple[float, float, float, float]]]] = {}
    for number, domain in enumerate(domains, 1):
        if domain.id.lower() != f"d{number:02d}":
            raise ValueError(f"第 {number} 个域名必须为 d{number:02d}")
        domain.id = domain.id.lower()
        if number == 1:
            if domain.parent_id != 0:
                raise ValueError("d01 parent_id 必须为 0")
            domain.parent_grid_ratio = 1
            domain.i_parent_start = domain.j_parent_start = 1
            continue
        if not 1 <= domain.parent_id < number:
            raise ValueError(f"{domain.id} 须指向已存在且编号更小的唯一父域；不能成环")
        if legacy and domain.parent_id != number - 1:
            raise ValueError("旧配置合同仅支持逐级嵌套；请创建 ui-2 新草稿使用嵌套树")
        parent = domains[domain.parent_id - 1]
        if parent.dx % domain.dx or parent.dy % domain.dy:
            raise ValueError(f"{domain.id} 网格距必须整除父域网格距")
        ratio_x, ratio_y = parent.dx // domain.dx, parent.dy // domain.dy
        if ratio_x != ratio_y or ratio_x not in {3, 5}:
            raise ValueError(f"{domain.id} dx/dy 嵌套比必须一致且为 3 或 5")
        if not legacy and domain.parent_grid_ratio not in {1, ratio_x}:
            raise ValueError(f"{domain.id} parent_grid_ratio 与真实父域网格距不一致")
        domain.parent_grid_ratio = ratio_x
        if (domain.e_we - 1) % ratio_x or (domain.e_sn - 1) % ratio_x:
            raise ValueError(f"{domain.id} 网格数减一必须能被嵌套比整除")
        west, south = domain.i_parent_start - 1, domain.j_parent_start - 1
        east = west + (domain.e_we - 1) / ratio_x
        north = south + (domain.e_sn - 1) / ratio_x
        if east > parent.e_we - 1 or north > parent.e_sn - 1:
            raise ValueError(f"{domain.id} 不能超出父域边界")
        for other, box in siblings.get(domain.parent_id, []):
            if min(east, box[2]) > max(west, box[0]) and min(north, box[3]) > max(south, box[1]):
                raise ValueError(f"同父兄弟域 {other.id} 与 {domain.id} 不能重叠")
        siblings.setdefault(domain.parent_id, []).append((domain, (west, south, east, north)))


PHYSICS_SUPPORTED_SCHEMES = {
    "mp_physics": {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 16, 17, 18, 28, 32, 50, 51, 55},
    "cu_physics": {0, 1, 2, 3, 5, 6, 7, 11, 14, 16, 93},
    "ra_lw_physics": {1, 3, 4, 5, 7, 14, 24, 31},
    "ra_sw_physics": {1, 3, 4, 5, 7, 14, 24, 31},
    "bl_pbl_physics": {0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12},
    "sf_sfclay_physics": {0, 1, 2, 5, 7, 10, 91},
    "sf_surface_physics": {0, 1, 2, 3, 4, 5, 7},
    "sf_urban_physics": {0, 1, 2, 3},
    "num_soil_layers": {4, 6, 9, 10},
    "num_land_cat": {20, 21, 24, 28, 33, 40},
}


class PhysicsConfig(BaseModel):
    preset: str = "默认通用"
    mp_physics: int = 8
    cu_physics: int = 0
    cu_physics_by_domain: list[int] | None = None
    ra_lw_physics: int = 4
    ra_sw_physics: int = 4
    bl_pbl_physics: int = 1
    sf_sfclay_physics: int = 1
    sf_surface_physics: int = 2
    sf_urban_physics: int = 0
    sf_urban_physics_by_domain: list[int] | None = None
    num_soil_layers: int = 4
    num_land_cat: int = 21
    radt: int = 5

    @model_validator(mode="after")
    def validate_supported_schemes(self):
        supported = {name: (choices, getattr(self, name)) for name, choices in PHYSICS_SUPPORTED_SCHEMES.items()}
        invalid = [name for name, (choices, value) in supported.items() if value not in choices]
        if invalid:
            raise ValueError(f"当前 WRF 环境不支持这些物理参数：{', '.join(invalid)}")
        if not 1 <= self.radt <= 60:
            raise ValueError("辐射调用间隔 radt 必须为 1-60 分钟")
        return self


class WrfTaskCreate(BaseModel):
    _provided_request: dict = PrivateAttr(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def preserve_provided_fields(cls, value, handler):
        # Provenance is presentation metadata, never a compute request field.
        from copy import deepcopy
        provided = deepcopy(value) if isinstance(value, dict) else None
        result = handler(value)
        if provided is not None:
            result._provided_request = provided
        return result

    configuration_profile: Literal["wrf-ordinary-ui-1", "wrf-ordinary-ui-2"] | None = None
    namelist: dict | None = None
    recovery_policy: Literal["confirm"] = "confirm"
    publish_to_catalog: bool = Field(default=True, strict=True)
    time_step_seconds: int | None = Field(default=None, ge=1)
    data_source: Literal["gfs", "ecmwf", "ec"] = "gfs"
    start_time: datetime
    end_time: datetime
    center: Center
    forecast_interval_hours: Literal[1, 3, 6] = 1
    history_interval_minutes: Literal[10, 30, 60, 180, 360] = 60
    domains: list[DomainConfig]
    spinup: SpinupConfig = Field(default_factory=SpinupConfig)
    vertical_levels: int = Field(default=DEFAULT_VERTICAL_LEVELS, ge=10, le=500, strict=True)
    physics: PhysicsConfig = Field(default_factory=PhysicsConfig)
    assimilation_scheme: Literal["off", "fdda_weak", "fdda_standard", "fdda_strong"] = "off"

    @model_validator(mode="before")
    @classmethod
    def current_profile_defaults(cls, value):
        if isinstance(value, dict) and value.get("configuration_profile") == CURRENT_CONFIGURATION_PROFILE:
            from configuration_profile import normalize
            return normalize(value)
        return value

    @model_validator(mode="after")
    def validate_request(self):
        if self.data_source == "ec":
            self.data_source = "ecmwf"
        source_label = self.data_source.upper()
        start = self.start_time
        end = self.end_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        else:
            start = start.astimezone(timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        else:
            end = end.astimezone(timezone.utc)
        self.start_time = start
        self.end_time = end
        if self.time_step_seconds is not None:
            if not self.domains or self.time_step_seconds > max(1, self.domains[0].dx // 1000 * 5):
                raise ValueError("自定义时间步长不得超过当前执行器的 5 秒/千米上限")
            if self.forecast_interval_hours * 3600 % self.time_step_seconds or self.history_interval_minutes * 60 % self.time_step_seconds:
                raise ValueError("时间步长必须整除边界资料和结果输出间隔")
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        if (end - start).total_seconds() > 30 * 86400:
            raise ValueError("模拟时间跨度不能超过 30 天")
        if (end - start).total_seconds() < self.forecast_interval_hours * 3600:
            raise ValueError(f"{source_label} 文件间隔不能大于模拟时长")
        if (end - start).total_seconds() < self.history_interval_minutes * 60:
            raise ValueError("结果输出间隔不能大于模拟时长")
        if (end - start).total_seconds() % (self.history_interval_minutes * 60):
            raise ValueError("模拟时长必须是结果输出间隔的整数倍")
        if any(value.minute or value.second or value.microsecond for value in (start, end)):
            raise ValueError("WRF 开始和结束时间必须为 UTC 整点")
        if start.hour % self.forecast_interval_hours or end.hour % self.forecast_interval_hours:
            raise ValueError(f"开始和结束时刻必须与 {source_label} 文件间隔对齐")
        validate_domain_tree(self.domains, legacy=self.configuration_profile == LEGACY_CONFIGURATION_PROFILE)
        allowed_surface = PBL_SURFACE_COMPATIBILITY.get(self.physics.bl_pbl_physics)
        if allowed_surface and self.physics.sf_sfclay_physics not in allowed_surface:
            raise ValueError("边界层方案与近地层方案不兼容")
        if self.physics.sf_surface_physics == 4 and self.physics.num_land_cat != 21:
            raise ValueError("Noah-MP 要求土地类型数为 21")
        if self.physics.sf_urban_physics == 1 and self.physics.sf_surface_physics not in {2, 4}:
            raise ValueError("UCM 城市冠层仅支持 Noah 或 Noah-MP 陆面方案")
        if self.physics.cu_physics_by_domain is not None:
            if len(self.physics.cu_physics_by_domain) != len(self.domains):
                raise ValueError("逐域积云方案数量必须与嵌套域数量一致")
            if any(value not in {0, 1, 2, 3, 5, 6, 7, 11, 14, 16, 93} for value in self.physics.cu_physics_by_domain):
                raise ValueError("逐域积云方案包含不支持的编号")
        if self.physics.sf_urban_physics_by_domain is not None:
            if len(self.physics.sf_urban_physics_by_domain) != len(self.domains):
                raise ValueError("逐域城市冠层方案数量必须与嵌套域数量一致")
            if any(value not in {0, 1} for value in self.physics.sf_urban_physics_by_domain):
                raise ValueError("逐域城市冠层方案仅支持 0 或 1")
            if 1 in self.physics.sf_urban_physics_by_domain and self.physics.sf_surface_physics not in {2, 4}:
                raise ValueError("逐域 UCM 城市冠层仅支持 Noah 或 Noah-MP 陆面方案")
        if self.data_source == "ecmwf" and self.forecast_interval_hours != 3:
            raise ValueError("ECMWF 全球开放资料为 3 小时边界场，请选择 3 小时间隔")
        hours = configured_spinup_hours(self.model_dump())
        if hours % self.forecast_interval_hours:
            raise ValueError("spin-up 小时数须为边界资料周期的整数倍")
        if hours == 0 and self.assimilation_scheme != "off":
            raise ValueError("spin-up 为 0 时不能启用 spin-up FDDA 网格松弛")
        model_start = start - timedelta(hours=hours)
        cycle_start = model_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if (end - cycle_start).total_seconds() / 3600 + 6 > 72:
            raise ValueError(f"模拟窗口、spin-up 与 6 小时边界缓冲超出 {source_label} f000-f072")
        outer = self.domains[0]
        meters_per_degree = 111_320.0
        longitude_scale = meters_per_degree * max(0.1, math.cos(math.radians(self.center.lat)))
        if (self.center.grid_x is None) != (self.center.grid_y is None):
            raise ValueError("网格参考点必须同时包含 x 和 y")
        if (self.center.grid_x or 1) > outer.e_we or (self.center.grid_y or 1) > outer.e_sn:
            raise ValueError("网格参考点必须位于 D01 内")
        half_width = (outer.e_we - 1) * outer.dx / longitude_scale / 2
        half_height = (outer.e_sn - 1) * outer.dy / meters_per_degree / 2
        safety_margin = 1.0
        west = self.center.lon - half_width - safety_margin
        east = self.center.lon + half_width + safety_margin
        reference_y = self.center.grid_y or (outer.e_sn + 1) / 2
        south = self.center.lat - (reference_y - 1) * outer.dy / meters_per_degree - safety_margin
        north = self.center.lat + (outer.e_sn - reference_y) * outer.dy / meters_per_degree + safety_margin
        if south < -90 or north > 90:
            raise ValueError("D01（含 1° 边界缓冲）不能越过极点；请缩小网格或调整中心纬度")
        # Do not constrain longitudes: a valid global domain can cross the date line.
        if self.namelist is not None:
            from namelist_config import resolve
            resolve(self.model_dump(mode="json"))
        return self


class HpcAuthRequest(BaseModel):
    password: SecretStr = Field(min_length=1, max_length=512)


class RemoteGfsCleanupRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=50)
    confirm: Literal[True]


class TaskDeleteRequest(BaseModel):
    confirm_task_id: str = Field(min_length=1)


class WrfTaskRestartRequest(BaseModel):
    request: WrfTaskCreate
    confirm_task_id: str = Field(min_length=1)
    confirm_attempt: int = Field(ge=1)


class RemoteGfsTriggerRequest(BaseModel):
    cycle: str = Field(pattern=r"^[0-9]{10}$")
