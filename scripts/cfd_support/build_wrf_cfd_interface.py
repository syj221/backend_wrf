#!/usr/bin/env python3
"""Build an auditable WRF forcing -> OpenFOAM boundary-interface package."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

G = 9.80665
RD_OVER_CP = 287.05 / 1004.0
C_MU = 0.09
FORCING_SCHEMA = "mesoscale-forcing/v2"
FORCING_TOPOLOGY = "normal-27km-to-wrf-les-10m/v1"
FORCING_DX = 27000.0 / (3 ** 7)


def destagger(values: np.ndarray, axis: int) -> np.ndarray:
    left = [slice(None)] * values.ndim
    right = [slice(None)] * values.ndim
    left[axis], right[axis] = slice(0, -1), slice(1, None)
    return 0.5 * (values[tuple(left)] + values[tuple(right)])


def read_time(ds: Dataset, index: int) -> str:
    values = np.asarray(ds.variables["Times"][index]).astype(str)
    return "".join(values).strip()


def qa(name: str, values: np.ndarray) -> dict:
    finite = np.isfinite(values)
    return {
        "name": name, "shape": list(values.shape), "nan_count": int(np.isnan(values).sum()),
        "finite_fraction": float(finite.mean()),
        "min": float(np.nanmin(values)), "max": float(np.nanmax(values)),
    }


def profile(z, u, v, w, theta, temperature, pressure, qvapor, tke, mask):
    rows = []
    for level in range(z.shape[0]):
        valid = mask[level] & np.isfinite(z[level])
        if not np.any(valid):
            continue
        speed = np.sqrt(u[level] ** 2 + v[level] ** 2 + w[level] ** 2)
        if tke is None:
            # Controlled diagnostic closure used when the selected profile does
            # not consume the WRF TKE/QKE field directly.
            k = 1.5 * (0.10 * speed) ** 2
        else:
            k = np.maximum(tke[level], 1e-6)
        mix_length = np.clip(0.1 * np.maximum(z[level], 5.0), 5.0, 100.0)
        epsilon = (C_MU ** 0.75) * np.maximum(k, 1e-6) ** 1.5 / mix_length
        rows.append({
            "z_agl_m": float(np.nanmean(z[level][valid])), "u_east_mps": float(np.nanmean(u[level][valid])),
            "v_north_mps": float(np.nanmean(v[level][valid])), "w_up_mps": float(np.nanmean(w[level][valid])),
            "theta_k": float(np.nanmean(theta[level][valid])), "temperature_k": float(np.nanmean(temperature[level][valid])),
            "pressure_pa": float(np.nanmean(pressure[level][valid])), "qvapor_kgkg": float(np.nanmean(qvapor[level][valid])),
            "k_m2s2": float(np.nanmean(k[valid])), "epsilon_m2s3": float(np.nanmean(epsilon[valid])),
        })
    return sorted(rows, key=lambda row: row["z_agl_m"])


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = ["z_agl_m", "u_east_mps", "v_north_mps", "w_up_mps", "theta_k", "temperature_k", "pressure_pa", "qvapor_kgkg", "k_m2s2", "epsilon_m2s3"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def canonical_digest(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_forcing_handoff(wrfout: Path, manifest_path: Path, valid_time: str) -> dict:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("约 10 m forcing manifest 无法读取") from exc
    if manifest.get("schema_version") != FORCING_SCHEMA or manifest.get("topology_id") != FORCING_TOPOLOGY:
        raise ValueError("CFD 入口只接受 mesoscale-forcing/v2 的 d08 forcing")
    if manifest.get("final_domain") != "d08" or abs(float(manifest.get("horizontal_spacing_m") or 0) - FORCING_DX) > 0.02:
        raise ValueError("forcing 入口网格不是 d08 · 12.345679 m")
    if manifest.get("quality_status") != "mesoscale_forcing_complete":
        raise ValueError("forcing 尚未通过 mesoscale 质量门")
    entries = manifest.get("forcing_files") or []
    matching = [entry for entry in entries if isinstance(entry, dict) and entry.get("valid_time_utc") == valid_time]
    if len(entries) < 2 or len(matching) != 1:
        raise ValueError("forcing 必须包含完整的逐小时文件清单，且 CFD 时次必须精确匹配其中一个文件")
    path = (manifest_path.parent / str(matching[0].get("path") or "")).resolve()
    if path != wrfout.resolve():
        raise ValueError("CFD 输入文件不是 manifest 精确指定的 d08 forcing 文件")
    if not path.is_file() or path.stat().st_size != int(matching[0].get("size_bytes") or 0):
        raise ValueError("CFD 输入 forcing 文件缺失或大小已变化")
    variables = {str(value).upper() for value in (manifest.get("variables") or [])}
    if not ({"TKE", "QKE"} & variables):
        raise ValueError("约 10 m CFD forcing 缺少 TKE/QKE，禁止回退固定湍流强度")
    expected_hash = str(matching[0].get("sha256") or "").lower()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if len(expected_hash) != 64 or digest != expected_hash:
        raise ValueError("CFD 输入 forcing SHA-256 不匹配")
    return {"schema_version": FORCING_SCHEMA, "topology_id": FORCING_TOPOLOGY, "manifest_sha256": canonical_digest(manifest), "valid_time_utc": valid_time, "entry": matching[0]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wrfout", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--time-index", type=int, default=0)
    parser.add_argument("--half-width-m", type=float, default=260.0, help="half-width of CFD forcing footprint")
    parser.add_argument("--forcing-manifest", type=Path, help="mesoscale-forcing/v2 manifest for the production d08 hand-off")
    parser.add_argument("--valid-time", help="exact forcing valid time, e.g. 2026-09-15T12:00:00Z")
    parser.add_argument("--turbulence-source", choices=("wrf_tke", "diagnostic_closure"), default="wrf_tke")
    parser.add_argument("--require-tke", action="store_true", help="reject diagnostic turbulence fallback")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = args.output_dir / "profiles"; profiles_dir.mkdir(exist_ok=True)

    forcing_handoff = None
    if args.forcing_manifest:
        if not args.valid_time:
            raise ValueError("使用 forcing manifest 时必须提供 --valid-time")
        forcing_handoff = validate_forcing_handoff(args.wrfout, args.forcing_manifest, args.valid_time)

    with Dataset(args.wrfout) as ds:
        index = args.time_index
        if index < 0 or index >= len(ds.dimensions["Time"]):
            raise ValueError("--time-index is outside WRF Time dimension")
        hgt = np.asarray(ds.variables["HGT"][index], dtype="float64")
        u = destagger(np.asarray(ds.variables["U"][index], dtype="float64"), -1)
        v = destagger(np.asarray(ds.variables["V"][index], dtype="float64"), -2)
        w = destagger(np.asarray(ds.variables["W"][index], dtype="float64"), 0)
        z = destagger((np.asarray(ds.variables["PH"][index]) + np.asarray(ds.variables["PHB"][index])) / G, 0) - hgt[None, :, :]
        theta = np.asarray(ds.variables["T"][index], dtype="float64") + 300.0
        pressure = np.asarray(ds.variables["P"][index], dtype="float64") + np.asarray(ds.variables["PB"][index], dtype="float64")
        temperature = theta * (pressure / 100000.0) ** RD_OVER_CP
        qvapor = np.asarray(ds.variables["QVAPOR"][index], dtype="float64") if "QVAPOR" in ds.variables else np.zeros_like(theta)
        tke_name = "TKE" if "TKE" in ds.variables else "QKE" if "QKE" in ds.variables else None
        if args.turbulence_source == "wrf_tke" and tke_name is None:
            raise ValueError("WRF 输出缺少 TKE/QKE")
        tke = np.asarray(ds.variables[tke_name][index], dtype="float64") if args.turbulence_source == "wrf_tke" and tke_name else None
        if tke_name == "QKE":
            # WRF QKE is twice the turbulent kinetic energy used by the CFD
            # closure; convert it before deriving epsilon.
            tke = 0.5 * tke
        cosalpha = np.asarray(ds.variables["COSALPHA"][index] if ds.variables["COSALPHA"].ndim == 3 else ds.variables["COSALPHA"][:], dtype="float64") if "COSALPHA" in ds.variables else np.ones_like(hgt)
        sinalpha = np.asarray(ds.variables["SINALPHA"][index] if ds.variables["SINALPHA"].ndim == 3 else ds.variables["SINALPHA"][:], dtype="float64") if "SINALPHA" in ds.variables else np.zeros_like(hgt)
        # WRF grid-relative -> earth-relative wind components.
        u, v = u * cosalpha[None, :, :] - v * sinalpha[None, :, :], v * cosalpha[None, :, :] + u * sinalpha[None, :, :]
        dx, dy = float(ds.DX), float(ds.DY)
        ny, nx = hgt.shape; center_y, center_x = ny // 2, nx // 2
        radius_x, radius_y = max(1, int(np.ceil(args.half_width_m / dx))), max(1, int(np.ceil(args.half_width_m / dy)))
        x0, x1 = max(0, center_x - radius_x), min(nx, center_x + radius_x + 1)
        y0, y1 = max(0, center_y - radius_y), min(ny, center_y + radius_y + 1)
        crop = (slice(None), slice(y0, y1), slice(x0, x1)); valid = np.isfinite(z[crop]) & (z[crop] >= 0)
        fields = {"u_east_mps": u[crop], "v_north_mps": v[crop], "w_up_mps": w[crop], "theta_k": theta[crop], "temperature_k": temperature[crop], "pressure_pa": pressure[crop], "z_agl_m": z[crop], "qvapor_kgkg": qvapor[crop]}
        masks = {"domain_mean": valid, "west": valid.copy(), "east": valid.copy(), "south": valid.copy(), "north": valid.copy()}
        masks["west"][:, :, 1:] = False; masks["east"][:, :, :-1] = False; masks["south"][:, 1:, :] = False; masks["north"][:, :-1, :] = False
        profile_meta = {}
        for name, mask in masks.items():
            rows = profile(z[crop], u[crop], v[crop], w[crop], theta[crop], temperature[crop], pressure[crop], qvapor[crop], tke[crop] if tke is not None else None, mask)
            if len(rows) < 2: raise RuntimeError(f"{name} profile has fewer than two usable levels")
            monotonic = all(b["z_agl_m"] > a["z_agl_m"] for a, b in zip(rows, rows[1:]))
            write_csv(profiles_dir / f"{name}.csv", rows)
            profile_meta[name] = {"file": f"profiles/{name}.csv", "levels": len(rows), "z_monotonic": monotonic}
        manifest = {"schema": "wrf-cfd-interface-v3" if forcing_handoff else "wrf-cfd-interface-v2", "source_wrfout": str(args.wrfout), "time": read_time(ds, index), "time_index": index, "wrf_grid": {"dx_m": dx, "dy_m": dy, "shape": [ny, nx]}, "crop": {"indices": {"x": [x0, x1], "y": [y0, y1]}, "requested_half_width_m": args.half_width_m, "actual_width_m": (x1 - x0 - 1) * dx, "actual_height_m": (y1 - y0 - 1) * dy}, "transformations": ["U/V destaggered", "W destaggered", "grid-relative U/V rotated to east/north with COSALPHA/SINALPHA", "p = P + PB", "theta = T + 300 K", "T = theta * (p/100000)^R/Cp", "z_agl = destagger((PH+PHB)/g) - HGT"], "forcing_handoff": forcing_handoff, "turbulence": {"source": "WRF TKE" if args.turbulence_source == "wrf_tke" and tke_name == "TKE" else "WRF QKE converted to TKE" if args.turbulence_source == "wrf_tke" and tke_name == "QKE" else "controlled diagnostic closure", "requested_source": args.turbulence_source, "fallback_used": tke is None, "epsilon": "C_mu^(3/4) k^(3/2) / clipped(0.1 z, 5, 100)"}, "qa": {"fields": [qa(name, value) for name, value in fields.items()], "profiles": profile_meta, "passed": all(item["nan_count"] == 0 for item in [qa(name, value) for name, value in fields.items()]) and all(item["z_monotonic"] for item in profile_meta.values()) and (not args.require_tke or tke is not None)}}
    (args.output_dir / "cfd_interface_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(args.output_dir / "cfd_interface_manifest.json"), "passed": manifest["qa"]["passed"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
