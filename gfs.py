from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import Settings


GFS_MAX_FORECAST_HOUR = 72


class GfsManager:
    """只负责 GFS 周期与时次计算；GRIB 文件始终由超算数据池管理。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def cycle_key(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y%m%d%H")

    def latest_cycles(self, now: datetime | None = None, count: int = 2) -> list[str]:
        cursor = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        return [self.cycle_key(cursor - timedelta(days=index)) for index in range(max(1, count))]

    def select_cycle(self, start: datetime, end: datetime) -> tuple[str, list[int]]:
        """按模型起报日固定选择 00Z；数据可用性由超算数据池负责检查。"""
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        candidate = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end_offset = int((end - candidate).total_seconds() // 3600) + 6
        start_offset = int((start - candidate).total_seconds() // 3600)
        if start_offset < 0 or end_offset > GFS_MAX_FORECAST_HOUR:
            raise RuntimeError("模拟窗口及 spin-up/边界缓冲超出单个 GFS 00Z 周期 f000-f072")
        return self.cycle_key(candidate), [start_offset, end_offset]

    def required_hours(self, start: datetime, end: datetime, interval: int, cycle: str) -> list[int]:
        cycle_time = datetime.strptime(cycle, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        first = int((start.astimezone(timezone.utc) - cycle_time).total_seconds() // 3600)
        last = int((end.astimezone(timezone.utc) - cycle_time).total_seconds() // 3600) + 6
        first = (first // interval) * interval
        last = ((last + interval - 1) // interval) * interval
        if first < 0 or last > GFS_MAX_FORECAST_HOUR:
            raise ValueError("模拟窗口及边界缓冲超出 GFS f000-f072")
        return list(range(first, last + 1, interval))
