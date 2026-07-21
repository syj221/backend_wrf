from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config import settings
from gfs import GfsManager


def test_latest_cycles_are_today_and_previous_day_at_00z() -> None:
    manager = GfsManager(settings)

    assert manager.latest_cycles(datetime(2026, 7, 21, 15, 30, tzinfo=timezone.utc)) == [
        "2026072100",
        "2026072000",
    ]


def test_required_hours_include_six_hour_boundary_buffer() -> None:
    manager = GfsManager(settings)
    start = datetime(2026, 7, 16, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 16, 6, tzinfo=timezone.utc)

    assert manager.required_hours(start, end, 3, "2026071600") == [0, 3, 6, 9, 12]


def test_select_cycle_is_deterministic() -> None:
    manager = GfsManager(settings)
    start = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    end = datetime(2026, 7, 16, 18, tzinfo=timezone.utc)

    cycle, offsets = manager.select_cycle(start, end)

    assert cycle == "2026071500"
    assert offsets == [12, 48]


def test_select_cycle_rejects_window_beyond_f072() -> None:
    manager = GfsManager(settings)
    start = datetime(2026, 7, 15, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 18, 0, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="f000-f072"):
        manager.select_cycle(start, end)


def test_required_hours_reject_negative_offset() -> None:
    manager = GfsManager(settings)
    start = datetime(2026, 7, 14, 23, tzinfo=timezone.utc)
    end = datetime(2026, 7, 15, 6, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="f000-f072"):
        manager.required_hours(start, end, 1, "2026071500")
