#!/usr/bin/env python3
"""Build checked browser layers and scientific products from OpenFOAM VTK slices."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
from pathlib import Path

import numpy as np
from netCDF4 import Dataset
from PIL import Image


def vtk_field(path: Path) -> tuple[str, np.ndarray, np.ndarray]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    point_line = next(i for i, line in enumerate(lines) if line.startswith("POINTS "))
    count = int(lines[point_line].split()[1])
    coordinate_values: list[str] = []
    index = point_line + 1
    while len(coordinate_values) < count * 3:
        coordinate_values.extend(lines[index].split())
        index += 1
    coords = np.asarray(coordinate_values[: count * 3], dtype="float64").reshape(count, 3)
    field_line = next(i for i, line in enumerate(lines) if line.startswith("FIELD attributes"))
    header = lines[field_line + 1].split()
    name, components, tuples = header[0], int(header[1]), int(header[2])
    if tuples != count:
        raise ValueError(f"{path}: field tuple count does not match POINTS")
    values: list[str] = []
    index = field_line + 2
    while len(values) < components * tuples:
        values.extend(lines[index].split())
        index += 1
    return name, coords, np.asarray(values[: components * tuples], dtype="float64").reshape(tuples, components)


def grid(coords: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    xs = np.arange(math.ceil(float(coords[:, 0].min())), math.floor(float(coords[:, 0].max())) + 1.0)
    ys = np.arange(math.ceil(float(coords[:, 1].min())), math.floor(float(coords[:, 1].max())) + 1.0)
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError("VTK slice is too small to rasterize")
    ix = np.clip(np.rint(coords[:, 0] - xs[0]).astype(int), 0, len(xs) - 1)
    iy = np.clip(np.rint(coords[:, 1] - ys[0]).astype(int), 0, len(ys) - 1)
    sums = np.zeros((len(ys), len(xs)), dtype="float64")
    counts = np.zeros((len(ys), len(xs)), dtype="int32")
    finite = np.isfinite(values)
    np.add.at(sums, (iy[finite], ix[finite]), values[finite])
    np.add.at(counts, (iy[finite], ix[finite]), 1)
    result = np.full_like(sums, np.nan)
    np.divide(sums, counts, out=result, where=counts > 0)
    return xs, ys, np.flipud(result)


def crop(array: np.ndarray, xs: np.ndarray, ys: np.ndarray, width_m: float, height_m: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    target_w = min(array.shape[1], max(2, int(round(width_m))))
    target_h = min(array.shape[0], max(2, int(round(height_m))))
    x0 = max(0, (array.shape[1] - target_w) // 2)
    y0 = max(0, (array.shape[0] - target_h) // 2)
    return array[y0:y0 + target_h, x0:x0 + target_w], xs[x0:x0 + target_w], ys[-(y0 + target_h):len(ys) - y0]


def webp(values: np.ndarray, cmap_name: str, path: Path) -> dict:
    finite = values[np.isfinite(values)]
    if not len(finite):
        raise ValueError("CFD layer has no finite values")
    vmin, vmax = np.percentile(finite, [2, 98])
    if vmax <= vmin:
        vmax = vmin + 1.0
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import colormaps
    norm = np.clip((values - vmin) / (vmax - vmin), 0, 1)
    rgba = (colormaps[cmap_name](norm) * 255).astype(np.uint8)
    rgba[~np.isfinite(values), 3] = 0
    Image.fromarray(rgba, "RGBA").save(path, "WEBP", lossless=True, method=6)
    return {
        "data_min": float(np.nanmin(values)), "data_max": float(np.nanmax(values)),
        "display_min_p02": float(vmin), "display_max_p98": float(vmax), "colormap": cmap_name,
    }


def write_tiff(path: Path, values: np.ndarray, x0: float, y0: float, pixel_m: float) -> None:
    """Write a small, standards-compliant float32 GeoTIFF without GDAL."""
    data = np.nan_to_num(values.astype("<f4"), nan=-9999.0).tobytes(order="C")
    width, height = values.shape[1], values.shape[0]
    entries = 16
    ifd_offset = 8
    ifd_size = 2 + entries * 12 + 4
    extra_offset = ifd_offset + ifd_size
    scale_offset = extra_offset
    tie_offset = scale_offset + 24
    geokey = struct.pack("<HHHHHHHHHHHH", 1, 1, 0, 4, 1024, 0, 1, 2, 1025, 0, 1, 1)
    geokey += struct.pack("<HHHHHHHH", 2048, 0, 1, 4326, 2054, 0, 1, 9102)
    geokey_offset = tie_offset + 48
    nodata = b"-9999.0\x00"
    nodata_offset = geokey_offset + len(geokey)
    strip_offset = nodata_offset + len(nodata)

    def entry(tag: int, kind: int, count: int, value: int) -> bytes:
        if kind == 3 and count == 1:
            return struct.pack("<HHI H", tag, kind, count, value) + b"\x00\x00"
        return struct.pack("<HHII", tag, kind, count, value)

    items = [
        entry(256, 4, 1, width), entry(257, 4, 1, height), entry(258, 3, 1, 32),
        entry(259, 3, 1, 1), entry(262, 3, 1, 1), entry(273, 4, 1, strip_offset),
        entry(277, 3, 1, 1), entry(278, 4, 1, height), entry(279, 4, 1, len(data)),
        entry(284, 3, 1, 1), entry(339, 3, 1, 3), entry(33550, 12, 3, scale_offset),
        entry(33922, 12, 6, tie_offset), entry(34735, 3, len(geokey) // 2, geokey_offset),
        entry(42113, 2, len(nodata), nodata_offset),
    ]
    path.write_bytes(
        b"II*\x00" + struct.pack("<I", ifd_offset) + struct.pack("<H", entries) + b"".join(items) +
        struct.pack("<I", 0) + struct.pack("<ddd", pixel_m, pixel_m, 0.0) +
        struct.pack("<dddddd", 0.0, 0.0, 0.0, x0, y0, 0.0) + geokey + nodata + data
    )


def write_netcdf(path: Path, layers: dict[str, np.ndarray], xs: np.ndarray, ys: np.ndarray, valid_time: str, height: float) -> None:
    with Dataset(path, "w", format="NETCDF4_CLASSIC") as ds:
        ds.Conventions = "CF-1.8"
        ds.title = "WRF to OpenFOAM representative CFD slice"
        ds.time_utc = valid_time
        ds.createDimension("y", len(ys)); ds.createDimension("x", len(xs))
        y = ds.createVariable("y", "f8", ("y",)); x = ds.createVariable("x", "f8", ("x",))
        y[:] = ys; x[:] = xs; x.units = "m"; y.units = "m"
        for name, values in layers.items():
            variable = ds.createVariable(name, "f4", ("y", "x"), zlib=True, fill_value=-9999.0)
            variable[:] = np.nan_to_num(values, nan=-9999.0); variable.coordinates = "x y"; variable.height_agl_m = height


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vtk_root", type=Path, help="directory containing z<N>Products/<time> slices")
    parser.add_argument("output", type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--valid-time", required=True)
    parser.add_argument("--aoi-center-lat", type=float, required=True)
    parser.add_argument("--aoi-center-lon", type=float, required=True)
    parser.add_argument("--width-m", type=float, required=True)
    parser.add_argument("--height-m", type=float, required=True)
    parser.add_argument("--heights", nargs="+", type=int, required=True)
    parser.add_argument("--solver-family", choices=("rans", "les"), required=True)
    parser.add_argument("--source-horizontal-crs", default="")
    parser.add_argument("--solver-log", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    preview_layers: dict[str, dict[str, str]] = {}
    scene_layers: dict[str, dict] = {}
    selected_height = None
    selected_arrays = None
    selected_xy = None
    for height in args.heights:
        directory = args.vtk_root / f"z{height}Products" / "latest"
        if not directory.is_dir():
            candidates = sorted((args.vtk_root / f"z{height}Products").glob("*/"))
            if not candidates:
                raise FileNotFoundError(f"missing VTK slice for {height} m")
            directory = candidates[-1]
        _, coords, u = vtk_field(directory / f"U_z{height}_products.vtk")
        _, t_coords, temperature = vtk_field(directory / f"T_z{height}_products.vtk")
        _, p_coords, pressure = vtk_field(directory / f"p_rgh_z{height}_products.vtk")
        if not (np.array_equal(coords, t_coords) and np.array_equal(coords, p_coords)):
            raise ValueError(f"VTK slices do not share coordinates at {height} m")
        xs, ys, speed = grid(coords, np.hypot(u[:, 0], u[:, 1]))
        _, _, temperature_c = grid(coords, temperature[:, 0] - 273.15)
        _, _, p_rgh = grid(coords, pressure[:, 0])
        source_xs, source_ys = xs, ys
        speed, xs, ys = crop(speed, source_xs, source_ys, args.width_m, args.height_m)
        temperature_c, _, _ = crop(temperature_c, source_xs, source_ys, args.width_m, args.height_m)
        p_rgh, _, _ = crop(p_rgh, source_xs, source_ys, args.width_m, args.height_m)
        height_dir = args.output / "preview" / str(height); height_dir.mkdir(parents=True, exist_ok=True)
        layer_info = {
            "wind_speed": {"file": f"preview/{height}/wind_speed.webp", "unit": "m s-1", "description": "horizontal wind speed", **webp(speed, "turbo", height_dir / "wind_speed.webp")},
            "temperature": {"file": f"preview/{height}/temperature.webp", "unit": "degC", "description": "temperature", **webp(temperature_c, "coolwarm", height_dir / "temperature.webp")},
            "p_rgh": {"file": f"preview/{height}/p_rgh.webp", "unit": "Pa", "description": "OpenFOAM p_rgh pressure field", **webp(p_rgh, "viridis", height_dir / "p_rgh.webp")},
        }
        preview_layers[str(height)] = {name: info["file"] for name, info in layer_info.items()}
        scene_layers[str(height)] = layer_info
        if selected_height is None or height == 40:
            selected_height, selected_arrays, selected_xy = height, {"wind_speed": speed, "temperature": temperature_c, "p_rgh": p_rgh}, (xs, ys)

    assert selected_arrays is not None and selected_xy is not None and selected_height is not None
    xs, ys = selected_xy
    meters_per_lat = 111_320.0
    meters_per_lon = meters_per_lat * max(0.1, abs(math.cos(math.radians(args.aoi_center_lat))))
    west = args.aoi_center_lon - args.width_m / 2 / meters_per_lon
    east = args.aoi_center_lon + args.width_m / 2 / meters_per_lon
    south = args.aoi_center_lat - args.height_m / 2 / meters_per_lat
    north = args.aoi_center_lat + args.height_m / 2 / meters_per_lat
    scientific = args.output / "scientific"; scientific.mkdir(exist_ok=True)
    geotiff = args.output / "geotiff"; geotiff.mkdir(exist_ok=True)
    vtk_dir = args.output / "vtk"; vtk_dir.mkdir(exist_ok=True)
    quality_dir = args.output / "quality"; quality_dir.mkdir(exist_ok=True)
    write_netcdf(scientific / "cfd_statistics.nc", selected_arrays, xs, ys, args.valid_time, selected_height)
    for name, values in selected_arrays.items():
        write_tiff(geotiff / f"{name}.tif", values, west, north, 1.0)
    source_dir = args.vtk_root / f"z{selected_height}Products" / "latest"
    if not source_dir.is_dir():
        source_dir = sorted((args.vtk_root / f"z{selected_height}Products").glob("*/"))[-1]
    for name in (f"U_z{selected_height}_products.vtk", f"T_z{selected_height}_products.vtk", f"p_rgh_z{selected_height}_products.vtk"):
        shutil_path = vtk_dir / name
        shutil_path.write_bytes((source_dir / name).read_bytes())
    scene_semantics = "representative_steady_snapshot" if args.solver_family == "rans" else "les_episode_statistics"
    solver_log = args.solver_log.read_text(encoding="utf-8", errors="replace") if args.solver_log and args.solver_log.is_file() else ""
    solver_warning = any(token in solver_log for token in ("FOAM Warning", "bounding ", "bounding entry"))
    scene = {
        "schema_version": "wrf-cfd-scene/v1", "task_id": args.task_id, "product_semantics": scene_semantics,
        "quality": {"status": "complete", "solver_warnings": solver_warning},
        "case_id": args.task_id, "height_m_agl": selected_height, "horizontal_resolution_m": 1.0,
        "grid": {"width": int(selected_arrays["wind_speed"].shape[1]), "height": int(selected_arrays["wind_speed"].shape[0]), "x_min_m": float(xs.min()), "x_max_m": float(xs.max()), "y_min_m": float(ys.min()), "y_max_m": float(ys.max()), "y_axis": "north_up"},
        "georeferencing": {"crs": "EPSG:4326", "source_horizontal_crs": args.source_horizontal_crs or None, "center": {"latitude": args.aoi_center_lat, "longitude": args.aoi_center_lon}, "bounds": {"west": west, "south": south, "east": east, "north": north}},
        "layers": scene_layers,
    }
    (args.output / "scene.meta.json").write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    quality = {"status": "complete", "solver_family": args.solver_family, "valid_time": args.valid_time, "height_m_agl": selected_height, "solver_warnings": solver_warning, "gates": [{"id": "openfoam_slice_export", "status": "review" if solver_warning else "pass", "message": "OpenFOAM solver log contains bounding or FOAM warnings." if solver_warning else None}, {"id": "browser_preview", "status": "pass"}, {"id": "georeferencing", "status": "review", "message": "EPSG:4326 bounds use the task AOI local metric approximation."}]}
    (quality_dir / "quality_report.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"product": str(args.output), "heights": args.heights, "selected_height": selected_height}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
