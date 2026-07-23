from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator


class Center(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


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
        supported = {
            "mp_physics": ({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 16, 17, 18, 28, 32, 50, 51, 55}, self.mp_physics),
            "cu_physics": ({0, 1, 2, 3, 5, 6, 7, 11, 14, 16, 93}, self.cu_physics),
            "ra_lw_physics": ({1, 3, 4, 5, 7, 14, 24, 31}, self.ra_lw_physics),
            "ra_sw_physics": ({1, 3, 4, 5, 7, 14, 24, 31}, self.ra_sw_physics),
            "bl_pbl_physics": ({0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12}, self.bl_pbl_physics),
            "sf_sfclay_physics": ({0, 1, 2, 5, 7, 10, 91}, self.sf_sfclay_physics),
            "sf_surface_physics": ({0, 1, 2, 3, 4, 5, 7}, self.sf_surface_physics),
            "sf_urban_physics": ({0, 1, 2, 3}, self.sf_urban_physics),
            "num_soil_layers": ({4, 6, 9, 10}, self.num_soil_layers),
            "num_land_cat": ({20, 21, 24, 28, 33, 40}, self.num_land_cat),
        }
        invalid = [name for name, (choices, value) in supported.items() if value not in choices]
        if invalid:
            raise ValueError(f"当前 WRF 环境不支持这些物理参数：{', '.join(invalid)}")
        if not 1 <= self.radt <= 60:
            raise ValueError("辐射调用间隔 radt 必须为 1-60 分钟")
        return self


class SpinupConfig(BaseModel):
    mode: Literal["off", "auto", "custom"] = "off"
    hours: Literal[0, 3, 6, 12, 18, 24] | None = None

    @model_validator(mode="after")
    def validate_hours(self):
        if self.mode == "custom" and self.hours is None:
            raise ValueError("自定义 spin-up 必须指定小时数")
        if self.mode == "off":
            self.hours = 0
        return self


class WrfTaskCreate(BaseModel):
    start_time: datetime
    end_time: datetime
    center: Center
    forecast_interval_hours: Literal[1, 3, 6, 12, 24] = 1
    domains: list[DomainConfig]
    physics: PhysicsConfig = Field(default_factory=PhysicsConfig)
    assimilation_scheme: Literal["off", "fdda_weak", "fdda_standard", "fdda_strong"] = "off"
    forecast_focus: Literal["general", "convection", "temperature_wind", "urban", "snowfall"] = "general"
    spinup: SpinupConfig = Field(default_factory=SpinupConfig)

    @model_validator(mode="after")
    def validate_request(self):
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
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        if (end - start).total_seconds() > 30 * 86400:
            raise ValueError("模拟时间跨度不能超过 30 天")
        if (end - start).total_seconds() < self.forecast_interval_hours * 3600:
            raise ValueError("GFS 文件间隔不能大于模拟时长")
        if any(value.minute or value.second or value.microsecond for value in (start, end)):
            raise ValueError("WRF 开始和结束时间必须为 UTC 整点")
        if start.hour % self.forecast_interval_hours or end.hour % self.forecast_interval_hours:
            raise ValueError("开始和结束时刻必须与 GFS 文件间隔对齐")
        if not 1 <= len(self.domains) <= 4:
            raise ValueError("嵌套域数量必须为 1-4")
        for index, domain in enumerate(self.domains, 1):
            expected_id = f"d{index:02d}"
            if domain.id.lower() != expected_id:
                raise ValueError(f"第 {index} 层域名必须为 {expected_id}")
            if index == 1:
                if domain.parent_id != 0:
                    raise ValueError("d01 parent_id 必须为 0")
                domain.parent_grid_ratio = 1
                domain.i_parent_start = 1
                domain.j_parent_start = 1
                continue
            parent = self.domains[index - 2]
            if domain.parent_id != index - 1:
                raise ValueError(f"{expected_id} parent_id 必须为 {index - 1}")
            if parent.dx % domain.dx or parent.dy % domain.dy:
                raise ValueError(f"{expected_id} 网格距必须整除父域网格距")
            ratio_x, ratio_y = parent.dx // domain.dx, parent.dy // domain.dy
            if ratio_x != ratio_y or ratio_x not in {3, 5}:
                raise ValueError(f"{expected_id} dx/dy 嵌套比必须一致且为 3 或 5")
            domain.parent_grid_ratio = ratio_x
            if (domain.e_we - 1) % ratio_x or (domain.e_sn - 1) % ratio_x:
                raise ValueError(f"{expected_id} 网格数减一必须能被嵌套比整除")
            end_i = domain.i_parent_start + (domain.e_we - 1) / ratio_x
            end_j = domain.j_parent_start + (domain.e_sn - 1) / ratio_x
            if end_i > parent.e_we or end_j > parent.e_sn:
                raise ValueError(f"{expected_id} 不能超出父域边界")
        pbl_surface = {1: {1}, 2: {2}, 5: {5}, 6: {5}, 7: {7}}
        allowed_surface = pbl_surface.get(self.physics.bl_pbl_physics)
        if allowed_surface and self.physics.sf_sfclay_physics not in allowed_surface:
            raise ValueError("边界层方案与近地层方案不兼容")
        if self.physics.sf_surface_physics == 4 and self.physics.num_land_cat != 21:
            raise ValueError("Noah-MP 要求土地类型数为 21")
        if self.physics.sf_urban_physics == 1 and self.physics.sf_surface_physics not in {2, 4}:
            raise ValueError("UCM 城市冠层仅支持 Noah 或 Noah-MP 陆面方案")
        if self.physics.cu_physics_by_domain is not None:
            if len(self.physics.cu_physics_by_domain) != len(self.domains):
                raise ValueError("逐域积云方案数量必须与嵌套域数量一致")
            if any(value < 0 for value in self.physics.cu_physics_by_domain):
                raise ValueError("逐域积云方案不能为负数")
        if self.physics.sf_urban_physics_by_domain is not None:
            if len(self.physics.sf_urban_physics_by_domain) != len(self.domains):
                raise ValueError("逐域城市冠层方案数量必须与嵌套域数量一致")
            if any(value not in {0, 1} for value in self.physics.sf_urban_physics_by_domain):
                raise ValueError("逐域城市冠层方案仅支持 0 或 1")
        if self.spinup.mode == "custom":
            spinup_hours = int(self.spinup.hours or 0)
            if spinup_hours % self.forecast_interval_hours:
                raise ValueError("自定义 spin-up 必须与 GFS 文件间隔对齐")
        elif self.spinup.mode == "auto":
            base = 12 if self.forecast_focus in {"convection", "urban", "snowfall"} or min(
                domain.dx for domain in self.domains
            ) <= 3000 else 6
            spinup_hours = next(
                (value for value in (6, 12, 18, 24) if value >= base and value % self.forecast_interval_hours == 0),
                24,
            )
        else:
            spinup_hours = 0
        model_start = start - timedelta(hours=spinup_hours)
        cycle_start = model_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if (end - cycle_start).total_seconds() / 3600 + 6 > 72:
            raise ValueError("模拟窗口、spin-up 与 6 小时边界缓冲超出 GFS f000-f072")
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


class WrfRecommendationRequest(BaseModel):
    center: Center
    domains: list[DomainConfig]
    forecast_focus: Literal["general", "convection", "temperature_wind", "urban", "snowfall"] = "general"
    start_time: datetime

    @model_validator(mode="after")
    def validate_domains(self):
        if not 1 <= len(self.domains) <= 4:
            raise ValueError("嵌套域数量必须为 1-4")
        return self
