from __future__ import annotations

from pathlib import Path

import numpy as np
from netCDF4 import Dataset

from renderer import render_run


def write_wrfout(path: Path, time_label: str) -> None:
    with Dataset(path, "w") as dataset:
        dataset.createDimension("Time", 1)
        dataset.createDimension("DateStrLen", 19)
        dataset.createDimension("south_north", 3)
        dataset.createDimension("west_east", 4)
        dataset.DX = 27000.0
        dataset.DY = 27000.0
        dataset.GRID_ID = 1
        times = dataset.createVariable("Times", "S1", ("Time", "DateStrLen"))
        times[0, :] = np.asarray(list(time_label), dtype="S1")
        lat = dataset.createVariable("XLAT", "f4", ("Time", "south_north", "west_east"))
        lon = dataset.createVariable("XLONG", "f4", ("Time", "south_north", "west_east"))
        lat[0] = np.linspace(30, 32, 12).reshape(3, 4)
        lon[0] = np.linspace(117, 120, 12).reshape(3, 4)
        t2 = dataset.createVariable("T2", "f4", ("Time", "south_north", "west_east"))
        t2.units = "K"
        t2.description = "2 metre temperature"
        t2[0] = np.linspace(270, 300, 12).reshape(3, 4)


def test_renderer_writes_webp_and_scene_manifest(tmp_path: Path) -> None:
    root = tmp_path
    raw_dir = root / "raw"
    output_dir = root / "data" / "WRF" / "runs" / "wrf_test"
    raw_dir.mkdir(parents=True)
    source = raw_dir / "wrfout_d01_2026-07-16_00:00:00"
    write_wrfout(source, "2026-07-16_00:00:00")
    manifest = render_run(
        "wrf_test",
        raw_dir,
        output_dir,
        {
            "start_time": "2026-07-16T00:00:00Z",
            "end_time": "2026-07-16T00:00:00Z",
            "center": {"lat": 31, "lon": 118},
            "domains": [{"id": "d01", "dx": 27000}],
        },
        "2026071600",
    )
    frame = manifest["domains"][0]["variables"][0]["frames"][0]
    assert frame["url"].endswith(".webp")
    assert (output_dir / "scene.meta.json").is_file()
    assert any((root / "data" / "WRF").rglob("*.webp"))
    assert manifest["quality"]["status"] == "complete"


def test_renderer_aligns_small_output_time_drift(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "data" / "WRF" / "runs" / "wrf_drift"
    raw_dir.mkdir(parents=True)
    write_wrfout(raw_dir / "wrfout_d01_2026-07-16_00:00:00", "2026-07-16_00:00:00")
    write_wrfout(raw_dir / "wrfout_d01_2026-07-16_01:00:45", "2026-07-16_01:00:45")

    manifest = render_run(
        "wrf_drift",
        raw_dir,
        output_dir,
        {
            "start_time": "2026-07-16T00:00:00Z",
            "end_time": "2026-07-16T01:00:00Z",
            "center": {"lat": 31, "lon": 118},
            "domains": [{"id": "d01", "dx": 27000}],
        },
        "2026071600",
    )

    quality = manifest["quality"]
    assert quality["status"] == "complete"
    assert quality["missing_times"] == {}
    assert quality["time_tolerance_seconds"] == 135
    assert quality["time_adjustments"] == [{
        "domain": "d01",
        "source_time": "2026-07-16T01:00:45Z",
        "time": "2026-07-16T01:00:00Z",
        "offset_seconds": 45,
    }]
    frames = manifest["domains"][0]["variables"][0]["frames"]
    assert frames[1]["time"] == "2026-07-16T01:00:00Z"
    assert frames[1]["source_time"] == "2026-07-16T01:00:45Z"


def test_renderer_keeps_large_output_time_drift_as_missing(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    output_dir = tmp_path / "data" / "WRF" / "runs" / "wrf_large_drift"
    raw_dir.mkdir(parents=True)
    write_wrfout(raw_dir / "wrfout_d01_2026-07-16_00:00:00", "2026-07-16_00:00:00")
    write_wrfout(raw_dir / "wrfout_d01_2026-07-16_01:03:00", "2026-07-16_01:03:00")

    manifest = render_run(
        "wrf_large_drift",
        raw_dir,
        output_dir,
        {
            "start_time": "2026-07-16T00:00:00Z",
            "end_time": "2026-07-16T01:00:00Z",
            "center": {"lat": 31, "lon": 118},
            "domains": [{"id": "d01", "dx": 27000}],
        },
        "2026071600",
    )

    quality = manifest["quality"]
    assert quality["status"] == "partial"
    assert quality["time_adjustments"] == []
    assert quality["missing_times"] == {"d01": ["2026-07-16T01:00:00Z"]}
