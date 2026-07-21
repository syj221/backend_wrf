from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

from renderer import render_run


def test_renderer_writes_webp_and_scene_manifest() -> None:
    root = Path("/tmp/zhihuiqixiang-backend-wrf-tests") / uuid.uuid4().hex
    raw_dir = root / "raw"
    output_dir = root / "data" / "WRF" / "runs" / "wrf_test"
    raw_dir.mkdir(parents=True)
    source = raw_dir / "wrfout_d01_2026-07-16_00:00:00"
    with Dataset(source, "w") as dataset:
        dataset.createDimension("Time", 1)
        dataset.createDimension("DateStrLen", 19)
        dataset.createDimension("south_north", 3)
        dataset.createDimension("west_east", 4)
        dataset.DX = 27000.0
        dataset.DY = 27000.0
        dataset.GRID_ID = 1
        times = dataset.createVariable("Times", "S1", ("Time", "DateStrLen"))
        times[0, :] = np.asarray(list("2026-07-16_00:00:00"), dtype="S1")
        lat = dataset.createVariable("XLAT", "f4", ("Time", "south_north", "west_east"))
        lon = dataset.createVariable("XLONG", "f4", ("Time", "south_north", "west_east"))
        lat[0] = np.linspace(30, 32, 12).reshape(3, 4)
        lon[0] = np.linspace(117, 120, 12).reshape(3, 4)
        t2 = dataset.createVariable("T2", "f4", ("Time", "south_north", "west_east"))
        t2.units = "K"
        t2.description = "2 metre temperature"
        t2[0] = np.linspace(270, 300, 12).reshape(3, 4)
    manifest = render_run(
        "wrf_test",
        raw_dir,
        output_dir,
        {"start_time": "2026-07-16T00:00:00Z", "end_time": "2026-07-16T06:00:00Z", "center": {"lat": 31, "lon": 118}},
        "2026071600",
    )
    frame = manifest["domains"][0]["variables"][0]["frames"][0]
    assert frame["url"].endswith(".webp")
    assert (output_dir / "scene.meta.json").is_file()
    assert any((root / "data" / "WRF").rglob("*.webp"))
