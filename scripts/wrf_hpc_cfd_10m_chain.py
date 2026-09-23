#!/usr/bin/env python3
"""Run the second phase: a validated d08 WRF-LES hand-off to CFD products.

This runner is deliberately separate from the d04-d08 WRF chain.  It consumes
one exact file from a completed ``mesoscale-forcing/v2`` task, validates the
whole requested hourly inventory before doing any work, and publishes only finite RANS
snapshots or finite LES statistics.  It never treats a WRF raster as a CFD
volume and never silently falls back to a guessed turbulence intensity.
"""

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


UTC = timezone.utc
FORCING_SCHEMA = "mesoscale-forcing/v2"
FORCING_TOPOLOGY = "normal-27km-to-wrf-les-10m/v1"
FORCING_DX = 27000.0 / (3 ** 7)
FATAL_LOG = re.compile(r"FOAM FATAL ERROR|\b(?:NaN|Inf)\b|segmentation fault|MPI_ABORT", re.I)


def env(name, default=""):
    return os.environ.get(name, default)


def quote(value):
    return shlex.quote(str(value))


def parse_time(value):
    normalized = str(value).replace("Z", "")
    parsed = datetime.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S")
    return parsed.replace(tzinfo=UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def stamp(value):
    return value.astimezone(UTC).strftime("%Y-%m-%d_%H:%M:%S")


def canonical_digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(value):
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or not path.name:
        raise ValueError("forcing manifest contains an unsafe path")
    return path


def parse_mtime(value):
    return parse_time(value).timestamp()


class Cfd10mChain:
    def __init__(self, contract_path, output_dir):
        self.contract_path = Path(contract_path)
        self.output_dir = Path(output_dir)
        self.contract = json.loads(self.contract_path.read_text(encoding="utf-8"))
        self.contract_sha256 = hashlib.sha256(self.contract_path.read_bytes()).hexdigest()
        self.task_id = str(self.contract.get("task_id") or "")
        self.upstream = self.contract.get("upstream") or {}
        self.upstream_mode = str(self.upstream.get("mode") or "")
        plan_domains = ((self.contract.get("execution_plan") or {}).get("wrf_les") or {}).get("domains") or []
        plan_final_dx = float((plan_domains[-1] or {}).get("dx_m") or 0) if plan_domains else 0.0
        self.composite_cfd = self.contract.get("execution_scope") == "wrf_cfd" and self.upstream_mode == "full_chain" and abs(plan_final_dx - FORCING_DX) <= 0.02
        self.source_forcing = self.upstream.get("source_forcing") or {}
        self.manifest = self.source_forcing.get("manifest") or {}
        self.source_root = Path(str(self.source_forcing.get("remote_output_dir") or ""))
        if self.composite_cfd:
            self.source_root = self.output_dir
            manifest_path = self.source_root / "mesoscale_forcing_manifest.json"
            if manifest_path.is_file():
                self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.source_forcing = {
                    "task_id": self.task_id,
                    "remote_output_dir": str(self.source_root),
                    "manifest": self.manifest,
                    "manifest_sha256": canonical_digest(self.manifest),
                }
        self.aoi = self.contract.get("publication_aoi") or {}
        self.cfd = self.contract.get("cfd") or {}
        self.parameterization = self.contract.get("parameterization") or {}
        final_parameters = self.parameterization.get("final_parameters") or {}
        if final_parameters and final_parameters.get("cfd") != self.cfd:
            raise ValueError("CFD 合同中的参数化方案与最终 cfd 参数不一致")
        self.domain = self.contract.get("derived_domain") or {}
        self.surface = self.contract.get("surface") or {"mode": "reviewed_terrain"}
        self.surface_dir = Path(env("WRF_DOWNSCALE_SURFACE_DIR", ""))
        self.surface_by_label = {}
        self.sampling = self.contract.get("sampling") or {}
        self.start = parse_time(str(self.upstream.get("start_time") or ""))
        self.end = parse_time(str(self.upstream.get("end_time") or "")) if self.upstream.get("end_time") else self.start + timedelta(hours=48)
        self.script_dir = Path(env("WRF_DOWNSCALE_CFD_SCRIPT_DIR", ""))
        self.python = Path(env("WRF_DOWNSCALE_PYTHON", sys.executable))
        self.template = Path(env("WRF_DOWNSCALE_CFD_TEMPLATE", ""))
        self.activate = Path(env("WRF_DOWNSCALE_OPENFOAM_ACTIVATE", ""))
        self.interface = self.script_dir / "build_wrf_cfd_interface.py"
        self.boundaries = self.script_dir / "apply_profile_boundaries.py"
        self.case_builder = self.script_dir / "wrf_hpc_cfd_case.py"
        self.products = self.script_dir / "wrf_hpc_cfd_products.py"
        self.root = self.output_dir / "cfd_10m"
        self.cases = self.root / "cases"
        self.product = self.output_dir / "product"
        self.stage_path = self.output_dir / "high-resolution-stage.json"
        self.marker_dir = self.root / "stage_markers"
        self.root.mkdir(parents=True, exist_ok=True)
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        self._validate()

    def _validate(self):
        if not self.task_id:
            raise ValueError("CFD contract has no task id")
        if self.contract.get("execution_scope") != "wrf_cfd" or self.upstream_mode not in {"reuse_10m", "full_chain"}:
            raise ValueError("10 m CFD runner only accepts execution_scope=wrf_cfd and upstream_mode=reuse_10m/full_chain")
        if not self.source_root.is_dir():
            raise FileNotFoundError("completed d08 forcing output directory is missing: %s" % self.source_root)
        expected_prefix = "%s/WRFwork/DOWNSCALE_" % env("WRF_STORAGE_ALIAS", "/home/tx-lab/.wrf-data").rstrip("/")
        if not str(self.source_root).startswith(expected_prefix) or not str(self.source_root).endswith("/run"):
            raise ValueError("CFD forcing input is outside the guarded data-volume layout")
        if not self.template.is_dir() or not self.activate.is_file():
            raise FileNotFoundError("OpenFOAM case template or activation script is missing")
        required = [self.interface, self.boundaries, self.case_builder, self.products]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError("CFD runner scripts are missing: " + ", ".join(missing))
        if self.manifest.get("schema_version") != FORCING_SCHEMA or self.manifest.get("topology_id") != FORCING_TOPOLOGY:
            raise ValueError("CFD requires a mesoscale-forcing/v2 d08 manifest")
        if (
            self.manifest.get("final_domain") != "d08"
            or int(self.manifest.get("domain_count") or 0) != 8
            or int(self.manifest.get("nominal_resolution_m") or 0) != 10
            or abs(float(self.manifest.get("horizontal_spacing_m") or 0) - FORCING_DX) > 0.02
        ):
            raise ValueError("CFD input is not the 12.345679 m d08 forcing")
        if self.manifest.get("quality_status") != "mesoscale_forcing_complete":
            raise ValueError("CFD input forcing has not passed its quality gate")
        if (self.manifest.get("quality") or {}).get("status") != "pass":
            raise ValueError("CFD input forcing quality gate is not pass")
        if canonical_digest(self.manifest) != str(self.source_forcing.get("manifest_sha256") or canonical_digest(self.manifest)):
            raise ValueError("forcing manifest hash in the contract does not match its content")
        self.manifest_path = self.source_root / "mesoscale_forcing_manifest.json"
        if not self.manifest_path.is_file():
            raise FileNotFoundError("completed d08 forcing manifest is missing")
        remote_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if canonical_digest(remote_manifest) != canonical_digest(self.manifest):
            raise ValueError("completed d08 forcing manifest changed after task hand-off")
        self.entries = self._validate_inventory()
        variables = {str(value).upper() for value in self.manifest.get("variables") or []}
        if not ({"TKE", "QKE"} & variables):
            raise ValueError("CFD requires WRF TKE/QKE; fixed-intensity fallback is disabled")
        required = {"U", "V", "W", "T", "P", "PB", "PH", "PHB", "QVAPOR", "HGT", "UST", "HFX", "PBLH"}
        if not required.issubset(variables):
            raise ValueError("CFD forcing manifest is missing required WRF boundary variables")
        if self.surface.get("mode") == "auto_flat" and not {"LU_INDEX", "ZNT"}.issubset(variables):
            raise ValueError("automatic flat surface requires LU_INDEX and ZNT in d08 forcing")
        if self.surface.get("mode") == "reviewed_terrain" and not any(self.surface_dir.glob("stl.*")):
            raise FileNotFoundError("reviewed terrain STL was not staged")
        vertical = self.manifest.get("vertical") or {}
        if int(self.manifest.get("vertical_layer_count") or vertical.get("layer_count") or 0) < 88:
            raise ValueError("CFD forcing must contain at least 88 vertical layers")
        if float(vertical.get("top_height_m") or 0) < float(self.domain.get("z_top_m_agl") or 0):
            raise ValueError("CFD forcing vertical coverage does not reach the CFD top")
        duration = int((self.end - self.start).total_seconds() // 3600)
        expected_times = [iso(self.start + timedelta(hours=index)) for index in range(duration + 1)]
        if self.manifest.get("valid_times_utc") != expected_times:
            raise ValueError("CFD forcing times do not match the requested time window")
        coverage = self.manifest.get("coverage") or {}
        extent = coverage.get("final_extent_m") or {}
        if float(extent.get("x") or 0) < float(self.domain.get("width_m") or 0) + 2 * FORCING_DX:
            raise ValueError("CFD forcing coverage is too narrow for the CFD domain")
        if float(extent.get("y") or 0) < float(self.domain.get("height_m") or 0) + 2 * FORCING_DX:
            raise ValueError("CFD forcing coverage is too short for the CFD domain")
        if self.manifest.get("horizontal_crs") and self.manifest.get("horizontal_crs") != self.aoi.get("horizontal_crs"):
            raise ValueError("CFD forcing CRS does not match the task AOI")
        self.family = str(self.cfd.get("solver_family") or "rans")
        if self.family not in {"rans", "les"}:
            raise ValueError("unsupported CFD solver family")
        self.thermal_mode = str(self.cfd.get("thermal_mode") or "thermal")
        if self.thermal_mode not in {"thermal", "neutral"}:
            raise ValueError("unsupported CFD thermal mode")
        self.solver_template_id = str(self.cfd.get("solver_template_id") or "managed-default/v1")
        if not re.fullmatch(r"[A-Za-z0-9_.+\-/]+", self.solver_template_id):
            raise ValueError("solver template id contains unsafe characters")
        turbulence = self.cfd.get("turbulence") or {}
        self.turbulence_source = str(turbulence.get("source") or "diagnostic_closure")
        if self.turbulence_source not in {"wrf_tke", "diagnostic_closure"}:
            raise ValueError("unsupported turbulence source")
        self.heights = sorted(set(int(value) for value in self.cfd.get("publication_heights_agl_m") or [10, 40, 80, 120, 200]))
        if not self.heights or any(value <= 0 for value in self.heights):
            raise ValueError("publication heights must be positive")
        if self.family == "les":
            self.solver = env("WRF_DOWNSCALE_LES_SOLVER", "buoyantPimpleFoam" if self.thermal_mode == "thermal" else "pimpleFoam")
        else:
            self.solver = env("WRF_DOWNSCALE_RANS_SOLVER", "buoyantSimpleFoam" if self.thermal_mode == "thermal" else "simpleFoam")
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", self.solver):
            raise ValueError("solver name contains unsafe characters")
        self.topology_sha256 = canonical_digest({
            "schema_version": self.manifest.get("schema_version"),
            "topology_id": self.manifest.get("topology_id"),
            "domain_count": self.manifest.get("domain_count"),
            "final_domain": self.manifest.get("final_domain"),
            "horizontal_spacing_m": self.manifest.get("horizontal_spacing_m"),
        })
        self.stage_config_sha256 = canonical_digest({
            "parameterization": self.parameterization,
            "cfd": self.cfd,
            "derived_domain": self.domain,
            "sampling": self.sampling,
            "solver": self.solver,
        })
        self.runner_assets_sha256 = canonical_digest({
            "scripts": {str(path.name): sha256_file(path) for path in [self.interface, self.boundaries, self.case_builder, self.products]},
            "activation": sha256_file(self.activate),
            "template": self._tree_digest(self.template),
        })

    @staticmethod
    def _tree_digest(root):
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append({"path": str(path.relative_to(root)), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
        return canonical_digest({"root": str(root), "files": files})

    def _validate_inventory(self):
        expected = list(self.manifest.get("valid_times_utc") or [])
        if len(expected) < 2 or len(set(expected)) != len(expected):
            raise ValueError("forcing manifest must contain unique hourly times")
        entries = list(self.manifest.get("forcing_files") or [])
        if len(entries) != len(expected):
            raise ValueError("forcing manifest file count does not match its hourly times")
        result = {}
        for expected_time, entry in zip(expected, entries):
            if entry.get("valid_time_utc") != expected_time:
                raise ValueError("forcing times are missing, duplicated or out of order")
            relative = safe_relative(entry.get("path"))
            path = (self.source_root / relative).resolve()
            try:
                path.relative_to(self.source_root.resolve())
            except ValueError:
                raise ValueError("forcing file resolves outside its managed output directory")
            if not path.is_file() or path.stat().st_size <= 0:
                raise ValueError("forcing file is missing: %s" % relative)
            if path.stat().st_size != int(entry.get("size_bytes") or 0):
                raise ValueError("forcing file size changed: %s" % relative)
            if sha256_file(path) != str(entry.get("sha256") or "").lower():
                raise ValueError("forcing file SHA-256 changed: %s" % relative)
            mtime = str(entry.get("mtime_utc") or "")
            if not mtime or abs(path.stat().st_mtime - parse_mtime(mtime)) > 1.0:
                raise ValueError("forcing file mtime changed: %s" % relative)
            result[expected_time] = path
        return result

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(path.name + ".staging")
        staging.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(staging, path)

    def _stage(self, status, next_stage, message, progress, **extra):
        payload = {
            "schema_version": "wrf-cfd-stage/v3", "task_id": self.task_id,
            "status": status, "next_stage": next_stage, "message": message,
            "progress": progress, "source_forcing_task_id": self.source_forcing.get("task_id"),
            "source_forcing_schema_version": FORCING_SCHEMA,
            "source_forcing_manifest_sha256": canonical_digest(self.manifest),
            "contract_sha256": self.contract_sha256, "target_dx_m": FORCING_DX,
            "cfd_not_started": False, "updated_at": iso(datetime.now(UTC)),
        }
        payload.update(extra)
        self._write(self.stage_path, payload)

    def _marker_path(self, name):
        return self.marker_dir / (name + ".json")

    def _mark(self, name, **extra):
        payload = {
            "schema_version": "wrf-cfd-stage-marker/v3", "stage": name,
            "contract_sha256": self.contract_sha256,
            "topology_sha256": self.topology_sha256,
            "forcing_manifest_sha256": canonical_digest(self.manifest),
            "input_inventory_sha256": canonical_digest(self.manifest.get("forcing_files") or []),
            "stage_config_sha256": self.stage_config_sha256,
            "runner_assets_sha256": self.runner_assets_sha256,
            "run_dir": str(self.root),
        }
        payload.update(extra)
        self._write(self._marker_path(name), payload)

    def _done(self, name):
        path = self._marker_path(name)
        if not path.is_file():
            return False
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        if (
            marker.get("contract_sha256") != self.contract_sha256
            or marker.get("topology_sha256") != self.topology_sha256
            or marker.get("forcing_manifest_sha256") != canonical_digest(self.manifest)
            or marker.get("input_inventory_sha256") != canonical_digest(self.manifest.get("forcing_files") or [])
            or marker.get("stage_config_sha256") != self.stage_config_sha256
            or marker.get("runner_assets_sha256") != self.runner_assets_sha256
            or marker.get("run_dir") != str(self.root)
        ):
            return False
        output = marker.get("output")
        if not output:
            return False
        output_path = (self.output_dir / str(output)).resolve()
        try:
            output_path.relative_to(self.output_dir.resolve())
        except ValueError:
            return False
        return output_path.is_dir() and (output_path / "scene.meta.json").is_file()

    def _archive_existing(self, path):
        if not path.exists():
            return
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = path.with_name(path.name + ".failed-attempt-" + suffix)
        index = 1
        while target.exists():
            target = path.with_name(path.name + ".failed-attempt-%s-%d" % (suffix, index))
            index += 1
        shutil.move(str(path), str(target))

    def run_python(self, script, *args):
        subprocess.run([str(self.python), str(script)] + [str(value) for value in args], cwd=str(self.root), check=True)

    def run_openfoam(self, case, command, log_name):
        log = case / (log_name + ".log")
        activation = "source %s || true" % quote(self.activate)
        shell = "set -e; %s; cd %s; %s" % (activation, quote(case), command)
        with log.open("ab") as handle:
            subprocess.run(["bash", "-lc", shell], stdout=handle, stderr=subprocess.STDOUT, check=True)
        text = log.read_text(encoding="utf-8", errors="replace")
        if FATAL_LOG.search(text):
            raise RuntimeError("OpenFOAM log contains a fatal or non-finite diagnostic: %s" % log)

    def write_export_dictionary(self, case):
        """Create the per-height VTK sampling functions after the case exists."""
        functions = []
        for height in self.heights:
            functions.append(
                """    z{height}Products
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
    }}""".format(height=height)
            )
        path = case / "system" / "export_products"
        path.write_text(
            """FoamFile { version 2.0; format ascii; class dictionary; object export_products; }
functions
{
""" + "\n".join(functions) + "\n}\n",
            encoding="utf-8",
        )
        return path

    def _valid_selection(self):
        if self.family == "rans":
            selected = (self.sampling.get("selected_valid_times") or [])
            if not selected:
                selected = [iso(self.start)]
            if len(selected) > 6:
                raise ValueError("RANS selection exceeds six snapshots")
            return [(str(value), None) for value in selected]
        episodes = self.sampling.get("episodes") or []
        if not episodes or len(episodes) > 2:
            raise ValueError("LES selection must contain one or two episodes")
        values = []
        for episode in episodes:
            hour = int(episode.get("forcing_hour") or 0)
            value = iso(self.start + timedelta(hours=hour))
            values.append((value, episode))
        return values

    def _surface_parameters(self, source):
        if self.surface.get("mode") == "reviewed_terrain":
            files = list(self.surface_dir.glob("surface_properties.*"))
            if len(files) != 1:
                raise ValueError("reviewed terrain requires one surface_properties file")
            value = json.loads(files[0].read_text(encoding="utf-8"))
            if value.get("schema_version") != "surface-properties/v1":
                raise ValueError("unsupported surface properties schema")
            z0 = float(value.get("roughness_length_m"))
            if not 1e-6 <= z0 <= 5:
                raise ValueError("reviewed terrain roughness is outside [1e-6, 5] m")
            return {"mode": "reviewed_terrain", "roughness_length_m": z0, "method": "reviewed_surface_properties/v1"}
        try:
            from netCDF4 import Dataset
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("automatic flat surface requires netCDF4 and numpy") from exc
        with Dataset(source) as dataset:
            for name in ("LU_INDEX", "ZNT", "XLAT", "XLONG"):
                if name not in dataset.variables:
                    raise ValueError("d08 forcing lacks surface variable %s" % name)
            lu = np.asarray(dataset.variables["LU_INDEX"][:]).squeeze()
            znt = np.asarray(dataset.variables["ZNT"][:]).squeeze()
            lat = np.asarray(dataset.variables["XLAT"][:]).squeeze()
            lon = np.asarray(dataset.variables["XLONG"][:]).squeeze()
            half_lat = float(self.aoi["height_m"]) / 2 / 111320
            half_lon = float(self.aoi["width_m"]) / 2 / (111320 * max(0.1, abs(math.cos(math.radians(float(self.aoi["center_lat"]))))))
            mask = (abs(lat-float(self.aoi["center_lat"])) <= half_lat) & (abs(lon-float(self.aoi["center_lon"])) <= half_lon)
            if not mask.any():
                nearest = np.unravel_index(np.argmin((lat-float(self.aoi["center_lat"]))**2 + (lon-float(self.aoi["center_lon"]))**2), lat.shape)
                mask = np.zeros(lat.shape, dtype=bool); mask[nearest] = True
            classes = [int(round(v)) for v in lu[mask].ravel() if np.isfinite(v)]
            if not classes:
                raise ValueError("LU_INDEX has no finite values in publication AOI")
            dominant = max(sorted(set(classes)), key=classes.count)
            values = [float(v) for v in znt[(mask) & (np.rint(lu) == dominant)].ravel() if np.isfinite(v) and 1e-6 <= float(v) <= 5]
            if not values:
                raise ValueError("ZNT has no valid values for the dominant LU_INDEX")
            return {"mode": "auto_flat", "landuse_index": dominant, "roughness_length_m": statistics.median(values), "method": "wrf_lu_index_znt_median/v1"}

    def _run_one(self, index, valid_time, episode):
        if valid_time not in self.entries:
            raise ValueError("selected CFD time is not an exact d08 forcing time: %s" % valid_time)
        label = "h%02d" % int(round((parse_time(valid_time) - self.start).total_seconds() / 3600.0))
        case = self.cases / label
        output = self.product / "snapshots" / label
        interface = self.root / "interfaces" / label
        if self._done("snapshot_" + label):
            return label, valid_time, episode
        if case.exists():
            self._archive_existing(case)
        if output.exists():
            self._archive_existing(output)
        if interface.exists():
            self._archive_existing(interface)
        interface.mkdir(parents=True, exist_ok=False)
        output.mkdir(parents=True, exist_ok=False)
        source = self.entries[valid_time]
        surface = self._surface_parameters(source)
        self.surface_by_label[label] = surface
        self._stage("surface_prepare", "cfd_1m_interface", "准备%s并确定地表粗糙度" % ("自动平坦地表" if surface["mode"] == "auto_flat" else "审核真实地形"), 65, valid_time=valid_time, snapshot=label, surface=surface)
        self._stage("cfd_1m_interface", "cfd_1m_mesh", "将 d08 forcing %s 转为 CFD 边界", 68, valid_time=valid_time, snapshot=label)
        interface_args = [self.interface, source, interface, "--forcing-manifest", self.manifest_path, "--valid-time", valid_time, "--turbulence-source", self.turbulence_source, "--half-width-m", max(float(self.domain.get("width_m") or 520) / 2.0, 260.0)]
        if self.turbulence_source == "wrf_tke":
            interface_args.append("--require-tke")
        self.run_python(*interface_args)
        steps = 2
        write_interval = 2
        if episode:
            steps = int(episode.get("spinup_seconds") or 0) + int(episode.get("statistics_window_seconds") or 60)
            write_interval = int(episode.get("output_interval_seconds") or 60)
        self._stage("cfd_1m_mesh", "cfd_1m_solver", "创建并检查 %s CFD 网格（%s）" % (label, self.family.upper()), 75, valid_time=valid_time, snapshot=label)
        case_args = [self.case_builder, self.template, case, interface / "profiles", "--width-m", self.domain.get("width_m") or self.aoi.get("width_m"), "--height-m", self.domain.get("height_m") or self.aoi.get("height_m"), "--z-top-m", self.domain.get("z_top_m_agl") or 384, "--surface-refinement-m", (self.domain.get("mesh_bands") or [{}])[0].get("max_cell_m") or 1, "--solver-steps", steps, "--write-interval", write_interval, "--solver-family", self.family, "--solver", self.solver, "--thermal-mode", self.thermal_mode, "--solver-template-id", self.solver_template_id, "--surface-mode", self.surface.get("mode") or "auto_flat", "--roughness-z0-m", surface["roughness_length_m"]]
        if self.surface.get("mode") == "reviewed_terrain":
            case_args.extend(["--surface-stl", next(self.surface_dir.glob("stl.*"))])
        if episode:
            case_args.extend(["--max-courant", episode.get("max_courant") or 0.8])
        self.run_python(*case_args)
        self.run_openfoam(case, "blockMesh", "mesh_block")
        self.run_openfoam(case, "surfaceFeatureExtract", "mesh_features")
        self.run_openfoam(case, "snappyHexMesh -overwrite", "mesh_snappy")
        self.run_openfoam(case, "checkMesh", "mesh_check")
        self.run_python(self.boundaries, case)
        self._stage("cfd_1m_solver", "cfd_1m_export", "运行 %s %s" % (self.family.upper(), label), 84, valid_time=valid_time, snapshot=label, solver=self.solver)
        self.run_openfoam(case, self.solver, "solver")
        self._stage("cfd_1m_export", "cfd_product_ready", "导出 %s 的多高度产品" % label, 92, valid_time=valid_time, snapshot=label)
        self.write_export_dictionary(case)
        self.run_openfoam(case, "postProcess -dict system/export_products -latestTime -fields '(T p_rgh U)'", "export")
        product_args = [case / "postProcessing", output, "--task-id", self.task_id, "--valid-time", valid_time, "--aoi-center-lat", self.aoi.get("center_lat"), "--aoi-center-lon", self.aoi.get("center_lon"), "--width-m", self.aoi.get("width_m"), "--height-m", self.aoi.get("height_m"), "--heights"]
        product_args.extend(self.heights)
        product_args.extend(["--solver-family", self.family, "--source-horizontal-crs", self.aoi.get("horizontal_crs") or "", "--solver-log", case / "solver.log"])
        self.run_python(self.products, *product_args)
        self._mark("snapshot_" + label, valid_time=valid_time, output=str(output.relative_to(self.output_dir)), solver=self.solver)
        return label, valid_time, episode

    def _write_product_manifest(self, selections):
        assets = []
        for path in sorted(self.product.rglob("*")):
            if not path.is_file():
                continue
            assets.append({"path": str(path.relative_to(self.product)), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
        labels = [item[0] for item in selections]
        first = labels[0]
        browser_previews = {}
        scene_data_by_label = {}
        for label, valid_time, _episode in selections:
            scene_path = "snapshots/%s/scene.meta.json" % label
            scene_data = json.loads((self.product / scene_path).read_text(encoding="utf-8"))
            scene_data_by_label[label] = scene_data
            layers = {}
            for height, values in (scene_data.get("layers") or {}).items():
                layers[str(height)] = {
                    name: "snapshots/%s/%s" % (label, info["file"])
                    for name, info in values.items()
                    if isinstance(info, dict) and info.get("file")
                }
            browser_previews[valid_time] = {"scene_manifest": scene_path, "layers": layers}
        first_scene = browser_previews[selections[0][1]]["scene_manifest"]
        first_scene_data = scene_data_by_label[first]
        scene_height = int(first_scene_data.get("height_m_agl") or self.heights[0])
        preview = browser_previews[selections[0][1]]["layers"]
        products = {
            "cf_netcdf": ["snapshots/%s/scientific/cfd_statistics.nc" % label for label in labels],
            "geotiff_layers": ["snapshots/%s/geotiff/%s.tif" % (label, name) for label in labels for name in ("wind_speed", "temperature", "p_rgh")],
            "vtk_scene": ["snapshots/%s/vtk/%s" % (label, name) for label in labels for name in ("U_z%d_products.vtk" % scene_height, "T_z%d_products.vtk" % scene_height, "p_rgh_z%d_products.vtk" % scene_height)],
            "quality_report": ["snapshots/%s/quality/quality_report.json" % label for label in labels],
        }
        for value in products.values():
            for path in value:
                if path not in {item["path"] for item in assets}:
                    raise RuntimeError("product asset missing from manifest: %s" % path)
        rans = []
        les = []
        for label, valid_time, episode in selections:
            if self.family == "rans":
                rans.append({"forcing": {"temporal_match": "exact", "valid_time": valid_time}, "solver": {"solution_type": "steady_rans", "solver": self.solver, "thermal_mode": self.thermal_mode, "solver_template_id": self.solver_template_id, "turbulence_source": self.turbulence_source}, "assets_prefix": "snapshots/%s" % label})
            else:
                start = parse_time(valid_time) + timedelta(seconds=int(episode.get("spinup_seconds") or 0))
                end = start + timedelta(seconds=int(episode.get("statistics_window_seconds") or 60))
                les.append({"forcing": {"temporal_match": "exact", "valid_time": valid_time}, "solver": {"solution_type": "les_time_window_statistics", "solver": self.solver, "thermal_mode": self.thermal_mode, "solver_template_id": self.solver_template_id, "turbulence_source": self.turbulence_source}, "statistics_window": {"start_utc": iso(start), "end_utc": iso(end), "output_interval_seconds": int(episode.get("output_interval_seconds") or 60), "max_courant": float(episode.get("max_courant") or 0.8), "inflow_method": episode.get("inflow_method")}, "assets_prefix": "snapshots/%s" % label})
        manifest = {
            "schema_version": "downscale-product/v2", "workflow_id": self.task_id,
            "product_kind": "wrf_to_cfd_rans_snapshot_set" if self.family == "rans" else "wrf_to_cfd_les_episode_set",
            "parameterization": self.contract.get("parameterization"),
            "surface_model": {**self.surface, "snapshots": self.surface_by_label, "product_label": "理想化平坦地表" if self.surface.get("mode") == "auto_flat" else "审核真实地形"},
            "configuration_hash": self.contract.get("configuration_hash"),
            "solver_configuration": {"family": self.family, "thermal_mode": self.thermal_mode, "solver": self.solver, "solver_template_id": self.solver_template_id, "turbulence_source": self.turbulence_source},
            "mesoscale_forcing": {"schema_version": FORCING_SCHEMA, "topology_id": FORCING_TOPOLOGY, "valid_times_utc": self.manifest.get("valid_times_utc"), "horizontal_spacing_m": FORCING_DX, "source_task_id": self.source_forcing.get("task_id"), "manifest_sha256": canonical_digest(self.manifest)},
            "publication_product": {"width_m": int(self.aoi["width_m"]), "height_m": int(self.aoi["height_m"]), "horizontal_sampling_m": 1},
            "temporal_semantics": {"kind": "independent_representative_steady_snapshots" if self.family == "rans" else "finite_les_time_window_statistics", "continuous_1m_time_series": False, "temporal_interpolation": "forbidden"},
            "rans_snapshots": rans, "les_episodes": les, "standard_products": products,
            "browser_preview": {"scene_manifest": first_scene, "layers": preview}, "browser_previews": browser_previews, "scene_3d_url": first_scene,
            "quality_gates": [{"id": "wrf_les_10m", "status": "pass"}, {"id": "forcing_manifest_integrity", "status": "pass"}, {"id": "cfd_mesh_and_solver", "status": "pass"}, {"id": "browser_product", "status": "pass"}],
            "assets": assets,
        }
        self._write(self.product / "downscale.product.json", manifest)
        return manifest

    def execute(self):
        selections = []
        requested = self._valid_selection()
        self._stage("source_forcing_handoff", "cfd_1m_interface", "验证已完成约 10 m forcing 的逐小时文件交接", 60, forcing_file_count=len(self.entries), solver_family=self.family)
        self._mark("source_forcing_handoff", file_count=len(self.entries))
        for index, (valid_time, episode) in enumerate(requested):
            label, value, episode = self._run_one(index, valid_time, episode)
            selections.append((label, value, episode))
        manifest = self._write_product_manifest(selections)
        self._stage("cfd_product_ready", "publish", "CFD 产品包已生成，等待后端质量校验与发布", 96, product_manifest="product/downscale.product.json", product_schema=manifest["schema_version"], solver_family=self.family)
        print("WRF_CFD_10M_PRODUCT_READY|task=%s|forcing=%s|solver=%s|snapshots=%d|output=%s" % (self.task_id, self.source_forcing.get("task_id"), self.family, len(selections), self.product))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", default=env("WRF_DOWNSCALE_CONTRACT"))
    parser.add_argument("--output", default=env("WRF_DOWNSCALE_OUTPUT_DIR"))
    args = parser.parse_args()
    try:
        chain = Cfd10mChain(args.contract, args.output)
        if env("WRF_DOWNSCALE_PREFLIGHT_ONLY", "false").lower() == "true":
            print("CFD_10M_PREFLIGHT_OK|forcing=%d|solver=%s|family=%s" % (len(chain.entries), chain.solver, chain.family))
            return 0
        chain.execute()
        return 0
    except Exception as exc:
        print("[10 m CFD 阶段失败] %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
