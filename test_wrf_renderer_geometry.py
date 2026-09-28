"""Small synthetic grid control points; not evidence of forecast skill."""

import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from netCDF4 import Dataset
from pyproj import CRS, Transformer

from renderer import _reproject_rgba, _rgba, render_run


class RendererGeometryTest(unittest.TestCase):
    def test_lambert_grid_reprojects_to_mercator_bounds(self):
        crs = CRS.from_proj4(
            "+proj=lcc +lat_1=30 +lat_2=30 +lat_0=30 +lon_0=105 +R=6370000 +units=m"
        )
        inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        x, y = np.meshgrid(np.arange(21) * 9000 - 90000, np.arange(17) * 9000 - 72000)
        lon, lat = inverse.transform(x, y)
        dataset = SimpleNamespace(MAP_PROJ=1, TRUELAT1=30, TRUELAT2=30,
                                  STAND_LON=105, DX=9000, DY=9000)
        image = np.zeros((17, 21, 4), dtype=np.uint8)
        image[:, :, 3] = 255
        corrected, bounds, error = _reproject_rgba(image, lat, lon, dataset)
        self.assertLess(error, 1)
        self.assertLess(bounds[0], 105)
        self.assertGreater(bounds[2], 105)
        self.assertLess(bounds[1], 30)
        self.assertGreater(bounds[3], 30)
        self.assertGreater(corrected.shape[0], 0)

    def test_common_colour_scale_is_reused(self):
        low, high = 250.0, 300.0
        first, _, _ = _rgba(np.array([[260.0, 270.0]]), "temperature", low, high)
        second, _, _ = _rgba(np.array([[270.0, 280.0]]), "temperature", low, high)
        np.testing.assert_array_equal(first[0, 1], second[0, 0])

    def test_missing_grid_value_is_transparent(self):
        image, _, _ = _rgba(np.array([[np.nan, 280.0]]), "temperature", 250.0, 300.0)
        self.assertEqual(int(image[0, 0, 3]), 0)
        self.assertEqual(int(image[0, 1, 3]), 185)

    def test_ten_minute_frames_keep_exact_times_and_common_scale(self):
        crs = CRS.from_proj4(
            "+proj=lcc +lat_1=30 +lat_2=30 +lat_0=30 +lon_0=105 +R=6370000 +units=m"
        )
        inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        x, y = np.meshgrid(np.arange(4) * 9000 - 13500, np.arange(3) * 9000 - 9000)
        lon, lat = inverse.transform(x, y)
        with tempfile.TemporaryDirectory() as temporary:
            task_id = "wrf_gfs_20260924T000000Z_1234abcd"
            output_dir = Path(temporary) / "runs" / task_id
            raw_dir = output_dir / "raw"
            raw_dir.mkdir(parents=True)
            start = datetime(2026, 9, 24, tzinfo=timezone.utc)
            for step in range(7):
                valid_time = start + timedelta(minutes=10 * step)
                stamp = valid_time.strftime("%Y-%m-%d_%H:%M:%S")
                with Dataset(str(raw_dir / f"wrfout_d01_{stamp}"), "w") as dataset:
                    dataset.createDimension("Time", 1)
                    dataset.createDimension("DateStrLen", 19)
                    dataset.createDimension("south_north", 3)
                    dataset.createDimension("west_east", 4)
                    dataset.MAP_PROJ = 1
                    dataset.TRUELAT1 = 30.0
                    dataset.TRUELAT2 = 30.0
                    dataset.STAND_LON = 105.0
                    dataset.DX = 9000.0
                    dataset.DY = 9000.0
                    dataset.createVariable("Times", "S1", ("Time", "DateStrLen"))[0] = np.frombuffer(stamp.encode(), dtype="S1")
                    dataset.createVariable("XLAT", "f8", ("Time", "south_north", "west_east"))[0] = lat
                    dataset.createVariable("XLONG", "f8", ("Time", "south_north", "west_east"))[0] = lon
                    variable = dataset.createVariable("T2", "f4", ("Time", "south_north", "west_east"))
                    variable.units = "K"
                    variable[0] = 270 + step + np.arange(12).reshape(3, 4)
            manifest = render_run(task_id, raw_dir, output_dir, {
                "data_source": "gfs", "start_time": "2026-09-24T00:00:00Z",
                "end_time": "2026-09-24T01:00:00Z", "history_interval_minutes": 10,
                "center": {"lat": 30, "lon": 105}, "domains": [{"id": "d01", "dx": 9000}],
            }, "2026092400")
            self.assertEqual(manifest["quality"]["status"], "complete")
            self.assertEqual(len(manifest["domains"][0]["times"]), 7)
            self.assertEqual(manifest["domains"][0]["times"][1], "2026-09-24T00:10:00Z")
            variable = manifest["domains"][0]["variables"][0]
            self.assertEqual(len({frame["display_min"] for frame in variable["frames"]}), 1)
            self.assertEqual(manifest["domains"][0]["geolocation"]["status"], "reprojected")
            self.assertTrue(any("RAINC/RAINNC" in note for note in manifest["quality"]["limitations"]))


if __name__ == "__main__":
    unittest.main()
