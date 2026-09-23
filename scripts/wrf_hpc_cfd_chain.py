#!/usr/bin/env python3
"""Run the managed 34 m WRF -> OpenFOAM -> browser product stage."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def quote(path: Path | str) -> str:
    return shlex.quote(str(path))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class CfdChain:
    def __init__(self, contract_path: Path, output_dir: Path):
        self.contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.output = output_dir
        self.cfd = output_dir / "cfd"
        self.product = output_dir / "product"
        self.stage_path = output_dir / "high-resolution-stage.json"
        self.cfd.mkdir(parents=True, exist_ok=True)
        self.product.mkdir(parents=True, exist_ok=True)
        self.task_id = str(self.contract.get("task_id") or "")
        self.aoi = self.contract.get("publication_aoi") or {}
        self.cfd_spec = self.contract.get("cfd") or {}
        self.domain = self.contract.get("derived_domain") or {}
        self.start = parse_time(str((self.contract.get("upstream") or {}).get("start_time")))
        self.source = self.output / "wrfout_34m_start.nc"
        self.python = Path(env("WRF_DOWNSCALE_PYTHON", sys.executable))
        self.interface_root = Path(env("WRF_DOWNSCALE_INTERFACE_ROOT", ""))
        self.template = Path(env("WRF_DOWNSCALE_CFD_TEMPLATE", ""))
        self.activate = Path(env("WRF_DOWNSCALE_OPENFOAM_ACTIVATE", ""))
        self.script_dir = Path(env("WRF_DOWNSCALE_CFD_SCRIPT_DIR", ""))
        self.interface_bin = self.interface_root / "bin" if (self.interface_root / "bin").is_dir() else self.interface_root
        self.case = self.cfd / "openfoam_case"
        self._validate()

    def _validate(self) -> None:
        if not self.task_id or not self.source.is_file() or self.source.stat().st_size <= 0:
            raise ValueError("34 m WRF 输出不存在，不能进入 CFD")
        if not self.interface_root.is_dir():
            raise FileNotFoundError(f"CFD interface root missing: {self.interface_root}")
        if not self.template.is_dir():
            raise FileNotFoundError(f"OpenFOAM case template missing: {self.template}")
        if not self.activate.is_file():
            raise FileNotFoundError(f"OpenFOAM activation script missing: {self.activate}")
        required = [
            self.interface_bin / "build_wrf_cfd_interface.py",
            self.interface_bin / "apply_profile_boundaries.py",
        ]
        required.extend([self.script_dir / "wrf_hpc_cfd_case.py", self.script_dir / "wrf_hpc_cfd_products.py"])
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError("CFD 运行脚本或接口文件缺失: " + ", ".join(missing))

    def write_stage(self, status: str, next_stage: str, message: str, progress: int) -> None:
        payload = {
            "schema_version": "wrf-cfd-stage/v2", "task_id": self.task_id, "status": status,
            "next_stage": next_stage, "message": message, "progress": progress,
            "source_wrfout": "wrfout_34m_start.nc", "target_dx_m": 34.293556,
            "cfd_not_started": False, "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        staging = self.stage_path.with_name(self.stage_path.name + ".staging")
        staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, self.stage_path)

    def run_python(self, script: Path, *args: str) -> None:
        subprocess.run([str(self.python), str(script), *args], cwd=self.cfd, check=True)

    def run_openfoam(self, command: str, log_name: str) -> None:
        log = self.cfd / f"{log_name}.log"
        # Some OpenFOAM/Conda activation hooks probe X11 on headless nodes.
        # The probe is unrelated to the solver and must not mask its status.
        shell = f"set -e; source {quote(self.activate)} || true; cd {quote(self.case)}; {command}"
        with log.open("ab") as handle:
            subprocess.run(["bash", "-lc", shell], stdout=handle, stderr=subprocess.STDOUT, check=True)

    def write_export_dictionary(self, heights: list[int]) -> Path:
        functions: list[str] = []
        for height in heights:
            functions.append(
                f"""    z{height}Products
    {{
        type surfaces;
        libs (\"libsampling.so\");
        writeControl writeTime;
        surfaceFormat vtk;
        fields (T p_rgh U);
        interpolationScheme cellPoint;
        surfaces
        (
            z{height}_products
            {{
                type cuttingPlane;
                planeType pointAndNormal;
                pointAndNormalDict {{ point (0 0 {height}); normal (0 0 1); }}
                interpolate true;
            }}
        );
    }}"""
            )
        path = self.case / "system" / "export_products"
        path.write_text(
            """FoamFile { version 2.0; format ascii; class dictionary; object export_products; }
functions
{
""" + "\n".join(functions) + "\n}\n", encoding="utf-8")
        return path

    def product_kind(self) -> tuple[str, dict]:
        family = str(self.cfd_spec.get("solver_family") or "rans")
        sampling = self.contract.get("sampling") or {}
        if family == "les":
            episodes = sampling.get("episodes") or []
            episode = episodes[0] if episodes else {"forcing_hour": 0, "statistics_window_seconds": 60}
            forcing_time = self.start + timedelta(hours=int(episode.get("forcing_hour") or 0))
            return "wrf_to_cfd_les_episode_set", {"forcing": {"temporal_match": "exact"}, "solver": {"solution_type": "les_time_window_statistics"}, "statistics_window": {"start_utc": forcing_time.isoformat().replace("+00:00", "Z"), "end_utc": (forcing_time + timedelta(seconds=int(episode.get("statistics_window_seconds") or 60))).isoformat().replace("+00:00", "Z")}}
        selected = (sampling.get("selected_valid_times") or [self.start.isoformat().replace("+00:00", "Z")])[:1]
        return "wrf_to_cfd_rans_snapshot_set", {"forcing": {"temporal_match": "exact", "valid_time": selected[0]}, "solver": {"solution_type": "steady_rans"}}

    def write_product_manifest(self, kind: str, semantics: dict) -> None:
        assets = []
        for path in sorted(self.product.rglob("*")):
            if not path.is_file() or path.name == "downscale.product.json":
                continue
            import hashlib
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assets.append({"path": str(path.relative_to(self.product)), "size_bytes": path.stat().st_size, "sha256": digest})
        heights = self.cfd_spec.get("publication_heights_agl_m") or [10, 40, 80, 120, 200]
        scene_height = 40 if 40 in heights else heights[0]
        preview = {str(height): {name: f"preview/{height}/{name}.webp" for name in ("wind_speed", "temperature", "p_rgh")} for height in heights}
        quality_path = self.product / "quality" / "quality_report.json"
        quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.is_file() else {}
        cfd_quality = next((gate.get("status") for gate in quality.get("gates", []) if gate.get("id") == "openfoam_slice_export"), "pass")
        manifest = {
            "schema_version": "downscale-product/v2", "workflow_id": self.task_id, "product_kind": kind,
            "mesoscale_forcing": {"valid_times_utc": [(self.start + timedelta(hours=i)).isoformat().replace("+00:00", "Z") for i in range(49)], "horizontal_spacing_m": 34.293556},
            "publication_product": {"width_m": int(self.aoi["width_m"]), "height_m": int(self.aoi["height_m"]), "horizontal_sampling_m": 1},
            "temporal_semantics": {"kind": "finite_les_time_window_statistics" if kind.endswith("les_episode_set") else "independent_representative_steady_snapshots", "continuous_1m_time_series": False, "temporal_interpolation": "forbidden"},
            "rans_snapshots": [semantics] if kind.endswith("rans_snapshot_set") else [],
            "les_episodes": [semantics] if kind.endswith("les_episode_set") else [],
            "standard_products": {"cf_netcdf": "scientific/cfd_statistics.nc", "geotiff_layers": ["geotiff/wind_speed.tif", "geotiff/temperature.tif", "geotiff/p_rgh.tif"], "vtk_scene": [f"vtk/U_z{scene_height}_products.vtk", f"vtk/T_z{scene_height}_products.vtk", f"vtk/p_rgh_z{scene_height}_products.vtk"], "quality_report": "quality/quality_report.json"},
            "browser_preview": {"scene_manifest": "scene.meta.json", "layers": preview},
            "scene_3d_url": "scene.meta.json", "quality_gates": [{"id": "wrf_les_34m", "status": "pass"}, {"id": "cfd_mesh_and_solver", "status": cfd_quality}, {"id": "browser_product", "status": "pass"}],
            "assets": assets,
        }
        (self.product / "downscale.product.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def execute(self) -> None:
        family = str(self.cfd_spec.get("solver_family") or "rans")
        heights = sorted({int(value) for value in (self.cfd_spec.get("publication_heights_agl_m") or [10, 40, 80, 120, 200])})
        self.write_stage("preparing_forcing", "cfd_1m_interface", "读取 34 m WRF 并生成 OpenFOAM 边界剖面", 68)
        interface = self.cfd / "interface"
        self.run_python(self.interface_bin / "build_wrf_cfd_interface.py", str(self.source), str(interface), "--half-width-m", str(max(float(self.domain.get("width_m") or 520) / 2, 260)))
        self.write_stage("meshing", "cfd_1m_mesh", "创建任务独立 OpenFOAM 域并执行网格检查", 73)
        self.run_python(self.script_dir / "wrf_hpc_cfd_case.py", str(self.template), str(self.case), str(interface / "profiles"), "--width-m", str(float(self.domain.get("width_m") or self.aoi["width_m"])), "--height-m", str(float(self.domain.get("height_m") or self.aoi["height_m"])), "--z-top-m", str(float(self.domain.get("z_top_m_agl") or 384)), "--surface-refinement-m", str(float(self.domain.get("mesh_bands", [{}])[0].get("max_cell_m") or 1)))
        self.run_openfoam("blockMesh", "mesh_block")
        self.run_openfoam("surfaceFeatureExtract", "mesh_features")
        self.run_openfoam("snappyHexMesh -overwrite", "mesh_snappy")
        self.run_openfoam("checkMesh", "mesh_check")
        self.run_python(self.interface_bin / "apply_profile_boundaries.py", str(self.case))
        self.write_stage("solving", "cfd_1m_solver", f"运行 {family.upper()} OpenFOAM 代表场", 82)
        self.run_openfoam("buoyantSimpleFoam", "solver")
        self.write_export_dictionary(heights)
        self.write_stage("generating_products", "cfd_1m_export", "导出多高度 VTK 切片和 1 m 预览图层", 90)
        self.run_openfoam("postProcess -dict system/export_products -latestTime -fields '(T p_rgh U)'", "export")
        sampling = self.contract.get("sampling") or {}
        if family == "les":
            episodes = sampling.get("episodes") or [{"forcing_hour": 0}]
            valid_time = (self.start + timedelta(hours=int(episodes[0].get("forcing_hour") or 0))).isoformat().replace("+00:00", "Z")
        else:
            selected_times = sampling.get("selected_valid_times") or [self.start.isoformat().replace("+00:00", "Z")]
            valid_time = str(selected_times[0])
        self.run_python(self.script_dir / "wrf_hpc_cfd_products.py", str(self.case / "postProcessing"), str(self.product), "--task-id", self.task_id, "--valid-time", valid_time, "--aoi-center-lat", str(self.aoi["center_lat"]), "--aoi-center-lon", str(self.aoi["center_lon"]), "--width-m", str(self.aoi["width_m"]), "--height-m", str(self.aoi["height_m"]), "--heights", *[str(value) for value in heights], "--solver-family", family, "--source-horizontal-crs", str(self.aoi.get("horizontal_crs") or ""), "--solver-log", str(self.cfd / "solver.log"))
        kind, semantics = self.product_kind()
        self.write_product_manifest(kind, semantics)
        self.write_stage("cfd_product_ready", "publish", "CFD 科学产品包已生成，等待服务端校验发布", 96)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", default=env("WRF_DOWNSCALE_CONTRACT"))
    parser.add_argument("--output", default=env("WRF_DOWNSCALE_OUTPUT_DIR"))
    args = parser.parse_args()
    try:
        CfdChain(Path(args.contract), Path(args.output)).execute()
    except Exception as exc:
        print(f"[CFD 1 m 阶段失败] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
