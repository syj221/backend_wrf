from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

from config import Settings


FORECAST_MAX_HOUR = 72
LEGACY_BOUNDARY_BUFFER_HOURS = 6


def boundary_buffer_hours(configuration_profile: str | None) -> int:
    """Only ui-2 removes the historical extra six-hour boundary window."""
    return 0 if configuration_profile == "wrf-ordinary-ui-2" else LEGACY_BOUNDARY_BUFFER_HOURS


def _check_buffer(buffer_hours: int) -> None:
    if type(buffer_hours) is not int or buffer_hours < 0:
        raise ValueError("边界缓冲须为非负整数小时")


class GfsManager:
    """负责 GFS/ECMWF 的统一周期与时次计算；GRIB 文件由 tx-lab 数据池管理。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def cycle_key(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y%m%d%H")

    @staticmethod
    def normalize_source(data_source: str) -> str:
        source = str(data_source or "gfs").strip().lower()
        if source == "ec":
            source = "ecmwf"
        if source not in {"gfs", "ecmwf"}:
            raise ValueError("数据源必须是 gfs 或 ecmwf")
        return source

    def latest_cycles(
        self,
        now: datetime | None = None,
        count: int = 1,
        data_source: str = "gfs",
    ) -> list[str]:
        self.normalize_source(data_source)
        # Publication is determined by the pool's f072 probe, not a guessed
        # fixed delay.  The queue keeps an unpublished current cycle in
        # waiting_publish while the previous complete cycles remain usable.
        cursor = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        return [self.cycle_key(cursor - timedelta(days=index)) for index in range(max(1, count))]

    def select_cycle(
        self,
        start: datetime,
        end: datetime,
        now: datetime | None = None,
        data_source: str = "gfs",
        *,
        buffer_hours: int = LEGACY_BOUNDARY_BUFFER_HOURS,
    ) -> tuple[str, list[int]]:
        """Select the nominal newest 00Z; the task manager verifies pool readiness."""
        source = self.normalize_source(data_source)
        _check_buffer(buffer_hours)
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        requested_cycle = start.replace(hour=0, minute=0, second=0, microsecond=0)
        available_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        latest_available_cycle = available_at.replace(hour=0, minute=0, second=0, microsecond=0)
        candidate = min(requested_cycle, latest_available_cycle)
        end_offset = math.ceil((end - candidate).total_seconds() / 3600) + buffer_hours
        start_offset = int((start - candidate).total_seconds() // 3600)
        if start_offset < 0 or end_offset > FORECAST_MAX_HOUR:
            raise RuntimeError(
                f"模拟窗口及 spin-up/边界缓冲超出单个 {source.upper()} 00Z 周期 f000-f072"
            )
        return self.cycle_key(candidate), [start_offset, end_offset]

    def required_hours(
        self,
        start: datetime,
        end: datetime,
        interval: int,
        cycle: str,
        data_source: str = "gfs",
        *,
        buffer_hours: int = LEGACY_BOUNDARY_BUFFER_HOURS,
    ) -> list[int]:
        source = self.normalize_source(data_source)
        _check_buffer(buffer_hours)
        if type(interval) is not int or interval <= 0:
            raise ValueError("驱动资料周期须为正整数小时")
        cycle_time = datetime.strptime(cycle, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        first = math.floor((start.astimezone(timezone.utc) - cycle_time).total_seconds() / (interval * 3600)) * interval
        last = math.ceil(((end.astimezone(timezone.utc) - cycle_time).total_seconds() / 3600 + buffer_hours) / interval) * interval
        if first < 0 or last > FORECAST_MAX_HOUR:
            raise ValueError(f"模拟窗口及边界缓冲超出 {source.upper()} f000-f072")
        return list(range(first, last + 1, interval))
