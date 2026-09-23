#!/usr/bin/env python3
"""Run the managed normal-WRF d04 -> WRF-LES d08 forcing chain.

The normal WRF task owns d01-d04 (27 km, 9 km, 3 km and 1 km).  This runner
only prepares the child nesting fields and executes four independent ndown /
WRF stages: d04->d05->d06->d07->d08.  The final product is a complete hourly
forcing inventory. A production full-chain contract then hands the same
immutable d08 forcing directly to the managed CFD runner.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


UTC = timezone.utc
TOPOLOGY_ID = "normal-27km-to-wrf-les-10m/v1"
SCHEMA_VERSION = "mesoscale-forcing/v2"
LES_PROFILE_ID = "wrf-3d-tke-les/v1"
DEFAULT_STORAGE_UUID = "7EEE64E3EE6494E5"
WRF_LES_IOFIELDS_TEXT = """# WRF-LES forcing diagnostics; stream 0 is the main history stream.
# Registry names are case-sensitive and must not contain spaces.
+:h:0:tke
"""
FATAL_LOG = re.compile(r"\bFATAL\b|\bNaN\b|\bInf\b|segmentation fault|CFL(?:\s+error|\s+violation|\s+exceed|\s+unstable)", re.I)
TIME_IN_NAME = re.compile(r"_(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})$")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def csv(values: Iterable[Any]) -> str:
    return ", ".join(str(value) for value in values)


def parse_start(value: str) -> datetime:
    normalized = value.replace("Z", "")
    parsed = datetime.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S")
    return parsed.replace(tzinfo=UTC)


def parse_utc_timestamp(value: str) -> datetime:
    """Parse the second-precision UTC timestamps emitted by the hand-off."""
    normalized = value.replace("Z", "")
    parsed = datetime.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S")
    return parsed.replace(tzinfo=UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d_%H:00:00")


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_forcing_inventory(root: Path, entries: List[Dict[str, Any]], expected_times: List[str]) -> None:
    """Validate the immutable hourly forcing inventory before publishing/resume."""
    if len(entries) != len(expected_times):
        raise RuntimeError(f"forcing 文件数量不完整：需要 {len(expected_times)} 个，实际 {len(entries)} 个")
    actual_times = [str(item.get("valid_time_utc") or "") for item in entries]
    if actual_times != list(expected_times) or len(set(actual_times)) != len(actual_times):
        raise RuntimeError("forcing 时次缺失、重复或顺序不连续")
    seen_paths = set()
    root_resolved = root.resolve()
    for item in entries:
        relative = Path(str(item.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"forcing 清单路径非法：{relative}")
        if str(relative) in seen_paths:
            raise RuntimeError(f"forcing 清单存在重复文件：{relative}")
        seen_paths.add(str(relative))
        path = (root / relative).resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError:
            raise RuntimeError(f"forcing 文件越出任务目录：{relative}")
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"forcing 文件缺失或为空：{relative}")
        size = int(item.get("size_bytes") or 0)
        if size != path.stat().st_size:
            raise RuntimeError(f"forcing 文件大小发生变化：{relative}")
        digest = str(item.get("sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or sha256_file(path) != digest:
            raise RuntimeError(f"forcing 文件 SHA-256 不匹配：{relative}")
        mtime_text = str(item.get("mtime_utc") or "")
        if not mtime_text:
            raise RuntimeError(f"forcing 文件缺少修改时间：{relative}")
        try:
            expected_mtime = parse_utc_timestamp(mtime_text).timestamp()
        except (TypeError, ValueError):
            raise RuntimeError(f"forcing 文件修改时间格式非法：{relative}")
        if abs(path.stat().st_mtime - expected_mtime) > 1.0:
            raise RuntimeError(f"forcing 文件修改时间发生变化：{relative}")


class Chain:
    def __init__(self, contract_path: Path, task_dir: Path, output_dir: Path):
        self.contract_path = contract_path
        self.task_dir = task_dir
        self.output_dir = output_dir
        self.root = output_dir / "chain"
        self.wps = self.root / "wps"
        self.real = self.root / "real"
        self.root.mkdir(parents=True, exist_ok=True)
        self.wps.mkdir(parents=True, exist_ok=True)
        self.real.mkdir(parents=True, exist_ok=True)

        self.contract_bytes = contract_path.read_bytes()
        self.contract = json.loads(self.contract_bytes.decode("utf-8"))
        plan = self.contract.get("execution_plan") or {}
        self.wrf_les = plan.get("wrf_les") or {}
        self.domains = list(self.wrf_les.get("domains") or [])
        self._validate_topology()
        self.execution_scope = str(self.contract.get("execution_scope") or "forcing_only")
        self.upstream_mode = str((self.contract.get("upstream") or {}).get("mode") or "")
        target_dx = float(self.domains[-1].get("dx_m") or 0) if self.domains else 0.0
        self.composite_cfd = (
            self.execution_scope == "wrf_cfd"
            and self.upstream_mode == "full_chain"
            and abs(target_dx - 27000.0 / (3 ** 7)) < 0.02
        )
        if self.execution_scope not in {"forcing_only", "wrf_cfd"} or (
            self.execution_scope == "wrf_cfd" and self.upstream_mode not in {"full_chain"}
        ):
            raise ValueError("WRF 分级链只接受 forcing_only 或 full_chain 组合高分任务")

        upstream = self.contract.get("upstream") or {}
        self.wrf = upstream.get("wrf") or {}
        self.source_artifact = upstream.get("source_artifact") or {}
        self.source_task = str(upstream.get("source_wrf_task_id") or "")
        self.source_domain = str((plan.get("source") or {}).get("final_domain") or "d04").lower()
        self.start = parse_start(str(upstream["start_time"]))
        forcing_contract = self.contract.get("forcing_contract") or {}
        configured_times = forcing_contract.get("required_valid_times") or forcing_contract.get("valid_times_utc")
        if configured_times:
            self.valid_times = [str(value) for value in configured_times]
            self.times = [parse_start(value) for value in self.valid_times]
        else:
            duration = int((forcing_contract.get("duration_hours") or 48))
            self.times = [self.start + timedelta(hours=index) for index in range(duration + 1)]
            self.valid_times = [iso(value) for value in self.times]
        if not self.times or self.times[0] != self.start:
            raise ValueError("forcing 合同的首个有效时次必须与普通 WRF 起报时间一致")
        self.end = self.times[-1]
        self.start_stamp = stamp(self.start)
        self.end_stamp = stamp(self.end)
        self.target_dx = float(self.domains[-1]["dx_m"])
        source_name = str(self.wrf.get("boundary_source") or "gfs")
        if source_name not in {"gfs", "ecmwf"}:
            raise ValueError(f"高分链仅支持 GFS 或 ECMWF 驱动，当前: {source_name}")
        self.source = "ec" if source_name == "ecmwf" else "gfs"

        self.storage_alias = Path(env("WRF_STORAGE_ALIAS", "/home/tx-lab/.wrf-data"))
        self.storage_mount = Path(env("WRF_STORAGE_MOUNT", ""))
        self.runtime_root = Path(env("WORK_DIR", str(self.storage_alias / "WRFwork/RUNTIME")))
        self.work_root = self.storage_alias / "WRFwork"
        data_root = env("WRF_EC_CACHE_ROOT" if self.source == "ec" else "WRF_GFS_DATA_ROOT")
        self.data_root = Path(data_root) if data_root else self.work_root / ("DATA/ECMWF" if self.source == "ec" else "DATA/GFS")
        source_path = str(self.source_artifact.get("remote_output_dir") or "")
        self.source_run = Path(source_path) if source_path else Path("__missing_source_run__")
        if not self.source_run.is_dir():
            candidates = [
                self.runtime_root / f"WRF_{self.source_task}/run",
                self.runtime_root / "RUNTIME" / f"WRF_{self.source_task}/run",
                self.storage_alias / "WRFwork/RUNTIME" / f"WRF_{self.source_task}/run",
            ]
            self.source_run = next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])
        self.wps_source = Path(env("WPS_SOURCE_DIR", "/home/tx-lab/WRFwork/WPS/WPS-4.6.0-nvhpc"))
        self.wrf_source = Path(env("WRF_CPU_SOURCE_DIR", "/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU"))
        self.runtime_env = Path(env("WRF_RUNTIME_ENV", "/home/tx-lab/WRFwork/env_wrf_nvhpc.sh"))
        self.geog = Path(env("GEOG_DATA_PATH", "/home/tx-lab/WRFwork/DATA/WPS_GEOG/WPS_GEOG"))
        self.mpi = int(env("WRF_MPI_PROCESSES", "4"))
        if not 1 <= self.mpi <= 64:
            raise ValueError("WRF_MPI_PROCESSES 超出受控范围")
        self.contract_sha256 = hashlib.sha256(self.contract_bytes).hexdigest()
        self.topology_sha256 = hashlib.sha256(self._canonical(self.domains)).hexdigest()
        self.stage_config_sha256 = hashlib.sha256(self._canonical({
            "profile_id": LES_PROFILE_ID,
            "iofields": WRF_LES_IOFIELDS_TEXT,
            "e_vert": int(self.wrf.get("e_vert", 88)),
        })).hexdigest()
        self.binary_hashes = {
            name: sha256_file(self.wrf_source / "main" / name)
            for name in ("wrf.exe", "real.exe", "ndown.exe")
        }
        self.max_cfl = None  # type: Optional[float]
        self.time_step_reduced = False
        self._check_storage()
        self._check_assets()
        self._check_source()

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    def _validate_topology(self) -> None:
        if len(self.domains) != 8:
            raise ValueError(f"约 10 m forcing 必须包含 d01-d08 8 个域，当前 {len(self.domains)} 个")
        for index, domain in enumerate(self.domains):
            expected = 27_000.0 / (3**index)
            if str(domain.get("name") or "").lower() != f"d{index + 1:02d}":
                raise ValueError(f"高分合同域名必须为 d{index + 1:02d}")
            if abs(float(domain.get("dx_m") or 0) - expected) > 0.02 or abs(float(domain.get("dy_m") or 0) - expected) > 0.02:
                raise ValueError(f"{domain['name']} 网格距不符合固定 3:1 拓扑")
            if index and int(domain.get("parent_grid_ratio") or 0) != 3:
                raise ValueError(f"{domain['name']} 的 parent_grid_ratio 必须为 3")
        if abs(float(self.domains[-1]["dx_m"]) - (27_000.0 / (3**7))) > 0.02:
            raise ValueError("d08 网格距不是约 10 m forcing 的 12.345679 m")

    def _check_storage(self) -> None:
        expected_uuid = env("WRF_STORAGE_UUID", DEFAULT_STORAGE_UUID).upper()
        if not self.storage_alias.exists() or not self.storage_alias.is_dir():
            raise RuntimeError(f"数据盘别名不存在: {self.storage_alias}")
        if self.storage_mount and self.storage_alias.resolve() != self.storage_mount.resolve():
            raise RuntimeError(f"数据盘别名未指向本次校验的 UUID 挂载点: {self.storage_alias}")
        usage = shutil.disk_usage(self.storage_alias)
        minimum_bytes = int(float(env("WRF_DOWNSCALE_MIN_FREE_GIB", "50")) * 1024**3)
        if usage.free < minimum_bytes:
            raise RuntimeError(f"数据盘剩余空间不足: {usage.free / 1024**3:.1f} GiB，至少需要 {minimum_bytes / 1024**3:.1f} GiB")
        system = shutil.disk_usage("/")
        system_minimum = int(float(env("WRF_DOWNSCALE_MIN_SYSTEM_FREE_GIB", "10")) * 1024**3)
        if system.free < system_minimum:
            raise RuntimeError(f"系统盘剩余空间不足: {system.free / 1024**3:.1f} GiB")
        # The shell guard has already resolved the block UUID. Keep the value
        # in the contract audit even when findmnt is unavailable to Python.
        self.storage_identity = {"expected_uuid": expected_uuid, "alias": str(self.storage_alias), "mount": str(self.storage_mount or self.storage_alias.resolve())}

    def _check_assets(self) -> None:
        required = [
            self.wps_source / "geogrid.exe", self.wps_source / "ungrib.exe", self.wps_source / "metgrid.exe",
            self.wrf_source / "main/real.exe", self.wrf_source / "main/wrf.exe", self.wrf_source / "main/ndown.exe", self.geog,
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise FileNotFoundError("高分链缺少运行资产: " + ", ".join(missing))
        for path in required[:-1]:
            if not os.access(path, os.X_OK):
                raise PermissionError(f"WRF/WPS 资产不可执行: {path}")

    def _source_names(self, prefix: str) -> Dict[str, Path]:
        return {
            iso_time: self.source_run / f"{prefix}_{self.source_domain}_{stamp(value)}"
            for iso_time, value in zip(self.valid_times, self.times)
        }

    def _check_source(self) -> None:
        if self.source_domain != "d04":
            raise ValueError(f"高分 forcing 父域必须是普通 WRF d04，合同为 {self.source_domain}")
        if not self.source_run.is_dir():
            raise FileNotFoundError(f"普通 WRF d04 原生输出目录不存在: {self.source_run}")
        history = self._source_names("wrfout")
        missing_history = [name for name, path in history.items() if not path.is_file() or path.stat().st_size <= 0]
        if missing_history:
            raise RuntimeError(f"普通 WRF d04 历史场缺少 {len(missing_history)} 个 forcing 时次，首个缺失 {missing_history[0]}")
        restart_names = self._source_names("wrfrst")
        missing_restart = [name for name, path in restart_names.items() if not path.is_file() or path.stat().st_size <= 0]
        if missing_restart:
            raise RuntimeError(f"普通 WRF d04 重启场缺少 {len(missing_restart)} 个逐小时文件，首个缺失 {missing_restart[0]}")
        source_identity = str(self.source_artifact.get("inventory_sha256") or "")
        if not source_identity:
            raise RuntimeError("普通 WRF d04 上游交接缺少 inventory_sha256")
        self._validate_source_inventory()
        self._scan_related_logs(self.source_run)
        first_history = next(iter(history.values()), None)
        if first_history is None:
            raise RuntimeError("普通 WRF d04 缺少可读取的历史场")
        try:
            self._validate_netcdf(first_history, self.domains[3])
        except Exception as exc:
            raise RuntimeError(f"普通 WRF d04 与高分 forcing 的 88 层垂直合同不一致: {exc}")

    def _validate_source_inventory(self) -> None:
        """Recheck every immutable source file before any stage can resume."""
        files = [item for item in (self.source_artifact.get("files") or []) if isinstance(item, dict)]
        if not files:
            raise RuntimeError("普通 WRF d04 上游交接缺少逐文件校验清单")
        for item in files:
            name = str(item.get("name") or "")
            if Path(name).name != name or not (name.startswith("wrfout_d04_") or name.startswith("wrfrst_d04_")):
                raise RuntimeError(f"普通 WRF d04 上游文件名非法：{name}")
            if not bool(item.get("stable")):
                raise RuntimeError(f"普通 WRF d04 上游文件仍在写入：{name}")
            path = self.source_run / name
            if not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeError(f"普通 WRF d04 上游文件缺失或为空：{name}")
            if int(item.get("size_bytes") or 0) != path.stat().st_size:
                raise RuntimeError(f"普通 WRF d04 上游文件大小变化：{name}")
            expected_hash = str(item.get("sha256") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or sha256_file(path) != expected_hash:
                raise RuntimeError(f"普通 WRF d04 上游文件 SHA-256 不匹配：{name}")
            mtime_text = str(item.get("mtime_utc") or "")
            try:
                expected_mtime = parse_utc_timestamp(mtime_text).timestamp()
            except (TypeError, ValueError):
                raise RuntimeError(f"普通 WRF d04 上游文件修改时间非法：{name}")
            if abs(path.stat().st_mtime - expected_mtime) > 1.0:
                raise RuntimeError(f"普通 WRF d04 上游文件修改时间变化：{name}")

    def _driver_cycle(self) -> Tuple[Path, int]:
        spinup = int(self.wrf.get("spinup_hours") or 6)
        target_cycle = self.start.strftime("%Y%m%d%H")
        if (self.data_root / target_cycle).is_dir():
            return self.data_root / target_cycle, 0
        source_cycle = str(self.source_artifact.get("source_cycle") or "")
        source_hours = [int(hour) for hour in (self.source_artifact.get("source_forecast_hours") or [])]
        if len(source_cycle) == 10 and source_cycle.isdigit() and source_hours:
            cycle = datetime.strptime(source_cycle, "%Y%m%d%H").replace(tzinfo=UTC)
            offset = (self.start - cycle).total_seconds() / 3600
            if not offset.is_integer() or offset < 0:
                raise ValueError("高分 forcing 起报时间早于或未对齐普通 WRF 驱动周期")
            forecast_start = int(offset)
            required = set(range(forecast_start, forecast_start + len(self.times)))
            if not required.issubset(set(source_hours)):
                missing = sorted(required - set(source_hours))
                raise FileNotFoundError(f"普通 WRF 驱动清单不能覆盖高分 forcing，缺少时效: {missing}")
            return self.data_root / source_cycle, forecast_start
        cycle = self.start - timedelta(hours=spinup)
        return self.data_root / cycle.strftime("%Y%m%d%H"), spinup

    def _driver_path(self, data_dir: Path, forecast_hour: int) -> Path:
        cycle_hour = data_dir.name[-2:]
        if self.source == "gfs":
            return data_dir / f"gfs.t{cycle_hour}z.pgrb2.0p25.f{forecast_hour:03d}"
        return data_dir / f"ecmwf.t{cycle_hour}z.ifs.0p25.f{forecast_hour:03d}.grib2"

    @staticmethod
    def _grib_link_name(index: int) -> str:
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        value = index
        digits = ["A", "A", "A"]
        for position in range(2, -1, -1):
            digits[position] = letters[value % 26]
            value //= 26
        return "GRIBFILE." + "".join(digits)

    def _link(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            raise RuntimeError(f"拒绝覆盖非链接目录: {target}")
        target.symlink_to(source)

    def _copy(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def _archive_incomplete(self, directory: Path, label: str) -> Optional[Path]:
        """Keep an interrupted stage intact before starting a clean retry."""
        if not directory.exists():
            return None
        stamp_text = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive = self.root / (label + ".failed-attempt-" + stamp_text)
        suffix = 1
        while archive.exists():
            archive = self.root / (label + ".failed-attempt-" + stamp_text + "-" + str(suffix))
            suffix += 1
        shutil.move(str(directory), str(archive))
        return archive

    def _link_wrf_runtime_files(self, directory: Path) -> None:
        """Expose the WRF run-time tables beside every executable invocation."""
        source_dir = self.wrf_source / "run"
        if not source_dir.is_dir():
            raise RuntimeError(f"WRF run-time资产目录不存在: {source_dir}")
        for source in source_dir.iterdir():
            # WRF writes namelist.output in the working directory. Never link
            # that mutable file back into the shared build directory.
            if source.name in {"namelist.input", "namelist.output"}:
                continue
            if source.is_file() or source.is_symlink():
                self._link(source, directory / source.name)

    def _write(self, path: Path, text: str) -> None:
        staging = path.with_name(path.name + ".staging")
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, path)

    def _marker_path(self, name: str) -> Path:
        return self.root / "stage_markers" / f"{name}.json"

    def _input_hash(self) -> str:
        # Keep the marker identity cheap to recompute while still making it
        # sensitive to the immutable source handoff and to input replacement.
        # The source inventory hash already commits the SHA-256 of every d04
        # file; mtime/size records catch a changed driver file without hashing
        # tens of gigabytes again on every stage probe.
        items: List[Dict[str, Any]] = [{
            "kind": "source_inventory",
            "sha256": str(self.source_artifact.get("inventory_sha256") or ""),
        }]
        for path in sorted(self.source_run.glob(f"wrfout_{self.source_domain}_*")) + sorted(self.source_run.glob(f"wrfrst_{self.source_domain}_*")):
            if path.is_file():
                stat = path.stat()
                items.append({"kind": "source_file", "name": path.name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        data_dir, forecast_start = self._driver_cycle()
        if not data_dir.is_dir():
            marker_path = self._marker_path("wps_prepare")
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                marker = {}
            frozen = str(marker.get("input_inventory_sha256") or "")
            if (
                frozen
                and marker.get("contract_sha256") == self.contract_sha256
                and marker.get("topology_sha256") == self.topology_sha256
                and marker.get("run_dir") == str(self.root)
                and self._stage_outputs_valid("wps_prepare")
            ):
                return frozen
            raise FileNotFoundError(f"驱动周期目录不存在且没有可复用的 WPS 检查点: {data_dir}")
        for index in range(len(self.times)):
            path = self._driver_path(data_dir, forecast_start + index)
            if path.is_file():
                stat = path.stat()
                items.append({"kind": "driver_file", "name": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            else:
                items.append({"kind": "missing_driver_file", "name": str(path)})
        return hashlib.sha256(self._canonical(items)).hexdigest()

    def done(self, name: str) -> bool:
        path = self._marker_path(name)
        if not path.is_file():
            return False
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        identities_match = (
            marker.get("contract_sha256") == self.contract_sha256
            and marker.get("topology_sha256") == self.topology_sha256
            and marker.get("input_inventory_sha256") == self._input_hash()
            and marker.get("wrf_binary_sha256") == self.binary_hashes
            and marker.get("stage_config_sha256") == self.stage_config_sha256
            and marker.get("run_dir") == str(self.root)
        )
        return identities_match and self._stage_outputs_valid(name)

    def _stage_outputs_valid(self, name: str) -> bool:
        if name == "source_handoff":
            return all(path.is_file() and path.stat().st_size > 0 for path in self._source_names("wrfout").values()) and all(
                path.is_file() and path.stat().st_size > 0 for path in list(self._source_names("wrfrst").values())[1:]
            )
        if name == "wps_prepare":
            return all((self.wps / f"geo_em.d{index:02d}.nc").is_file() and (self.wps / f"geo_em.d{index:02d}.nc").stat().st_size > 0 for index in range(1, 9)) and all(
                (self.wps / f"met_em.d{index:02d}.{stamp(value)}.nc").is_file() and (self.wps / f"met_em.d{index:02d}.{stamp(value)}.nc").stat().st_size > 0
                for index in range(1, 9) for value in self.times
            )
        if name == "real_prepare":
            return (self.real / "wrfbdy_d01").is_file() and (self.real / "wrfbdy_d01").stat().st_size > 0 and all(
                (self.real / f"wrfinput_d{index:02d}").is_file() and (self.real / f"wrfinput_d{index:02d}").stat().st_size > 0
                for index in range(1, 9)
            )
        if name.startswith("ndown_d"):
            match = re.fullmatch(r"ndown_d(\d{2})_d(\d{2})", name)
            if not match:
                return False
            directory = self.root / name
            return all((directory / filename).is_file() and (directory / filename).stat().st_size > 0 for filename in ("wrfinput_d02", "wrfbdy_d02"))
        if name.startswith("wrf_d"):
            match = re.fullmatch(r"wrf_d(\d{2})_(?:333m|111m|37m|10m)", name)
            if not match:
                return False
            directory = self.root / f"wrf_d{match.group(1)}"
            domain_index = int(match.group(1))
            target = self.domains[domain_index - 1]
            try:
                self._validate_wrf_stage_outputs(directory, target)
                return True
            except (OSError, RuntimeError, ValueError):
                return False
        if name == "forcing_ready":
            manifest_path = self.output_dir / "mesoscale_forcing_manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("topology_id") != TOPOLOGY_ID:
                    return False
                validate_forcing_inventory(self.output_dir, manifest.get("forcing_files") or [], self.valid_times)
                return True
            except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError):
                return False
        return False

    def mark(self, name: str) -> None:
        marker = {
            "schema_version": "wrf-les-stage-marker/v2",
            "stage": name,
            "task_id": self._task_id(),
            "contract_sha256": self.contract_sha256,
            "topology_sha256": self.topology_sha256,
            "input_inventory_sha256": self._input_hash(),
            "wrf_binary_sha256": self.binary_hashes,
            "stage_config_sha256": self.stage_config_sha256,
            "run_dir": str(self.root),
            "completed_at": iso(datetime.now(UTC)),
        }
        path = self._marker_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write(path, json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
        self._write(self.root / f"{name}.done", name + "\n")

    def _task_id(self) -> str:
        prefix = "DOWNSCALE_"
        name = self.output_dir.parent.name
        return name[len(prefix):] if name.startswith(prefix) else name

    @staticmethod
    def _shell_join(command: Iterable[str]) -> str:
        return " ".join(shlex.quote(str(item)) for item in command)

    def _run(self, cwd: Path, name: str, command: List[str]) -> None:
        cwd.mkdir(parents=True, exist_ok=True)
        log = cwd / f"{name}.log"
        command_line = self._shell_join(command)
        if self.runtime_env.is_file():
            command_line = f". {shlex.quote(str(self.runtime_env))} >/dev/null 2>&1 && exec {command_line}"
            wrapped = ["bash", "-lc", command_line]
        else:
            wrapped = command
        with log.open("ab") as handle:
            handle.write(("\n[managed-command] " + command_line + "\n").encode())
            try:
                subprocess.run(wrapped, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, check=True)
            except subprocess.CalledProcessError as exc:
                self._scan_log(log)
                self._scan_related_logs(cwd)
                raise RuntimeError(f"{name} 非正常结束，退出码 {exc.returncode}，日志: {log}") from exc
        self._scan_log(log)
        self._scan_related_logs(cwd)

    def _scan_log(self, log: Path) -> None:
        if not log.is_file():
            return
        text = log.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?:reduc|decreas|shrink|lower).*time step|time step.*(?:reduc|decreas|shrink|lower)", text, re.I):
            self.time_step_reduced = True
        bad = [line.strip() for line in text.splitlines() if FATAL_LOG.search(line)]
        if bad:
            raise RuntimeError(f"日志质量门失败 {log.name}: {bad[-1][:500]}")
        values = [float(match.group(1)) for match in re.finditer(r"CFL[^0-9+-]*([0-9]+(?:\.[0-9]+)?)", text, re.I)]
        if values:
            self.max_cfl = max(values) if self.max_cfl is None else max(self.max_cfl, max(values))

    def _scan_related_logs(self, cwd: Path) -> None:
        """Scan WRF MPI rank logs as well as the managed stdout log."""
        for pattern in ("rsl.error.*", "rsl.out.*"):
            for log in sorted(cwd.glob(pattern)):
                self._scan_log(log)

    def _common_time(self, count: int, start: Optional[datetime] = None) -> str:
        start = start or self.start
        values = lambda getter: csv([getter(start)] * count)
        return (
            f"run_days = {int((self.end - start).total_seconds() // 86400)}, run_hours = {int((self.end - start).total_seconds() // 3600) % 24},\n"
            f" start_year = {values(lambda value: value.year)},\n start_month = {values(lambda value: value.month)},\n start_day = {values(lambda value: value.day)},\n start_hour = {values(lambda value: value.hour)},\n"
            f" end_year = {csv([self.end.year] * count)},\n end_month = {csv([self.end.month] * count)},\n end_day = {csv([self.end.day] * count)},\n end_hour = {csv([self.end.hour] * count)},\n"
        )

    def _normal_physics(self, count: int) -> Tuple[List[int], List[int], List[int], List[int], List[int], List[int], List[int]]:
        physics = self.wrf.get("physics") or {}
        mp = int(physics.get("mp_physics") or 8)
        lw = int(physics.get("ra_lw_physics") or 4)
        sw = int(physics.get("ra_sw_physics") or 4)
        pbl = int(physics.get("bl_pbl_physics") or 1)
        sfclay = int(physics.get("sf_sfclay_physics") or 1)
        surface = int(physics.get("sf_surface_physics") or 2)
        radt = int(physics.get("radt_minutes") or 1)
        return ([mp] * count, [lw] * count, [sw] * count, [pbl] * count, [sfclay] * count, [surface] * count, [radt] * count)

    def _physics(self, les_flags: List[bool]) -> str:
        mp, lw, sw, normal_pbl, sfclay, surface, radt = self._normal_physics(len(les_flags))
        pbl = [0 if les else normal_pbl[index] for index, les in enumerate(les_flags)]
        cu_config = list((self.wrf.get("physics") or {}).get("cu_physics_by_domain") or [])
        cu = [0 if les else int(cu_config[index] if index < len(cu_config) else 0) for index, les in enumerate(les_flags)]
        return (
            f" mp_physics = {csv(mp)},\n"
            f" cu_physics = {csv(cu)},\n"
            f" ra_lw_physics = {csv(lw)},\n"
            f" ra_sw_physics = {csv(sw)},\n"
            f" bl_pbl_physics = {csv(pbl)},\n"
            f" sf_sfclay_physics = {csv(sfclay)},\n"
            f" sf_surface_physics = {csv(surface)},\n"
            f" radt = {csv(radt)},\n"
            f" isfflx = 1,\n ifsnow = 0,\n icloud = 1,\n ghg_input = 0,\n surface_input_source = 1,\n"
            f" num_soil_layers = 4,\n num_land_cat = 21,\n"
            f" sf_urban_physics = {csv([0] * len(les_flags))}\n"
        )

    def _dynamics(self, les_flags: List[bool]) -> str:
        diff = [2 if les else 1 for les in les_flags]
        km = [2 if les else 4 for les in les_flags]
        count = len(les_flags)
        return (
            f" hybrid_opt = 2, w_damping = 1, w_crit_cfl = 0.8, diff_opt = {csv(diff)}, km_opt = {csv(km)},\n"
            f" mix_full_fields = {csv(['.true.'] * count)},\n"
            f" non_hydrostatic = {csv(['.true.'] * count)}, moist_adv_opt = {csv([2] * count)}, scalar_adv_opt = {csv([2] * count)}\n"
        )

    def _wps_namelist(self) -> str:
        count = len(self.domains)
        source_center = self.source_artifact.get("source_center") or {}
        return f"""&share
 wrf_core = 'ARW',
 max_dom = {count},
 start_date = {csv([repr(self.start_stamp)] * count)},
 end_date = {csv([repr(self.end_stamp)] * count)},
 interval_seconds = 3600,
 io_form_geogrid = 2,
 debug_level = 0,
/
&geogrid
 parent_id = {csv([1] + list(range(1, count)))},
 parent_grid_ratio = {csv([1] + [int(item['parent_grid_ratio']) for item in self.domains[1:]])},
 i_parent_start = {csv([int(item['parent_start_i']) for item in self.domains])},
 j_parent_start = {csv([int(item['parent_start_j']) for item in self.domains])},
 e_we = {csv([int(item['e_we']) for item in self.domains])},
 e_sn = {csv([int(item['e_sn']) for item in self.domains])},
 geog_data_res = {csv([repr('default')] * count)},
 dx = {float(self.domains[0]['dx_m'])},
 dy = {float(self.domains[0]['dy_m'])},
 map_proj = 'lambert',
 ref_x = {source_center.get('grid_x') or (self.domains[0]['e_we'] + 1) / 2},
 ref_y = {source_center.get('grid_y') or (self.domains[0]['e_sn'] + 1) / 2},
 ref_lat = {float(((self.source_artifact.get('source_center') or {}).get('lat') or (self.contract.get('publication_aoi') or {}).get('center_lat')))},
 ref_lon = {float(((self.source_artifact.get('source_center') or {}).get('lon') or (self.contract.get('publication_aoi') or {}).get('center_lon')))},
 truelat1 = {float(source_center.get('lat') or self.contract['publication_aoi']['center_lat'])},
 truelat2 = {float(source_center.get('lat') or self.contract['publication_aoi']['center_lat'])},
 stand_lon = {float(((self.source_artifact.get('source_center') or {}).get('lon') or (self.contract.get('publication_aoi') or {}).get('center_lon')))},
 geog_data_path = '{self.geog}',
 opt_geogrid_tbl_path = './geogrid/',
/
&ungrib
 out_format = 'WPS',
 prefix = 'FILE',
/
&metgrid
 fg_name = 'FILE',
 io_form_metgrid = 2,
 opt_metgrid_tbl_path = './metgrid/',
/
"""

    def wps_prepare(self) -> None:
        if self.done("wps_prepare"):
            return
        data_dir, forecast_start = self._driver_cycle()
        if not data_dir.is_dir():
            raise FileNotFoundError(f"驱动周期目录不存在: {data_dir}")
        for child in ("geogrid", "metgrid", "ungrib"):
            (self.wps / child).mkdir(parents=True, exist_ok=True)
        self._link(self.wps_source / "geogrid/GEOGRID.TBL", self.wps / "geogrid/GEOGRID.TBL")
        self._link(self.wps_source / "metgrid/METGRID.TBL", self.wps / "metgrid/METGRID.TBL")
        for exe in ("geogrid.exe", "ungrib.exe", "metgrid.exe"):
            self._link(self.wps_source / exe, self.wps / exe)
        if self.source == "gfs":
            vtable = self.wps_source / "ungrib/Variable_Tables/Vtable.GFS"
        else:
            vtable = self.wps_source / "ungrib/Variable_Tables/Vtable.ECMWF"
        self._link(vtable, self.wps / "Vtable")
        for index in range(len(self.times)):
            source = self._driver_path(data_dir, forecast_start + index)
            if not source.is_file() or source.stat().st_size <= 0:
                raise FileNotFoundError(f"缺少驱动文件: {source}")
            self._link(source, self.wps / self._grib_link_name(index))
        self._write(self.wps / "namelist.wps", self._wps_namelist())
        self._run(self.wps, "geogrid", ["./geogrid.exe"])
        self._run(self.wps, "ungrib", ["./ungrib.exe"])
        self._run(self.wps, "metgrid", ["./metgrid.exe"])
        for index in range(1, len(self.domains) + 1):
            domain = f"d{index:02d}"
            if not (self.wps / f"geo_em.{domain}.nc").is_file():
                raise RuntimeError(f"geogrid 缺少 {domain}")
            for value in self.times:
                if not (self.wps / f"met_em.{domain}.{stamp(value)}.nc").is_file():
                    raise RuntimeError(f"metgrid 缺少 {domain} 的 {iso(value)} 时次")
        self.mark("wps_prepare")

    def real_prepare(self) -> None:
        if self.done("real_prepare"):
            return
        count = len(self.domains)
        for index in range(1, count + 1):
            for source in self.wps.glob(f"met_em.d{index:02d}.*.nc"):
                self._link(source, self.real / source.name)
        self._link(self.wrf_source / "main/real.exe", self.real / "real.exe")
        self._link_wrf_runtime_files(self.real)
        ratios = [1] + [int(item["parent_grid_ratio"]) for item in self.domains[1:]]
        les_flags = [index >= 4 for index in range(count)]
        namelist = f"""&time_control
{self._common_time(count)}
 interval_seconds = 3600,
 input_from_file = {csv(['.true.'] * count)},
 history_interval = {csv([60] * count)},
 restart = .false., restart_interval = 60,
 io_form_history = 2, io_form_restart = 2, io_form_input = 2, io_form_boundary = 2,
/
&domains
 time_step = 90,
 max_dom = {count},
 e_we = {csv([int(item['e_we']) for item in self.domains])},
 e_sn = {csv([int(item['e_sn']) for item in self.domains])},
 e_vert = {csv([int(self.wrf.get('e_vert', 88))] * count)},
 p_top_requested = 5000,
 num_metgrid_levels = 34,
 num_metgrid_soil_levels = 4,
 dx = {csv([float(item['dx_m']) for item in self.domains])},
 dy = {csv([float(item['dy_m']) for item in self.domains])},
 grid_id = {csv(range(1, count + 1))},
 parent_id = {csv([0] + list(range(1, count)))},
 i_parent_start = {csv([int(item['parent_start_i']) for item in self.domains])},
 j_parent_start = {csv([int(item['parent_start_j']) for item in self.domains])},
 parent_grid_ratio = {csv(ratios)},
 parent_time_step_ratio = {csv(ratios)},
 feedback = 0, smooth_option = 0,
/
&physics
{self._physics(les_flags)}
/
&dynamics
{self._dynamics(les_flags)}
/
&bdy_control
 spec_bdy_width = 5,
 specified = {csv(['.true.'] + ['.false.'] * (count - 1))},
 nested = {csv(['.false.'] + ['.true.'] * (count - 1))},
/
&namelist_quilt
 nio_tasks_per_group = 0,
 nio_groups = 1,
/
"""
        self._write(self.real / "namelist.input", namelist)
        self._run(self.real, "real", ["mpirun", "-np", str(self.mpi), "./real.exe"])
        for index in range(1, count + 1):
            domain = f"d{index:02d}"
            for filename in (f"wrfinput_{domain}", f"wrfndi_{domain}"):
                if filename.startswith("wrfndi_"):
                    continue
                if not (self.real / filename).is_file() or (self.real / filename).stat().st_size <= 0:
                    raise RuntimeError(f"real 缺少 {filename}")
        if not (self.real / "wrfbdy_d01").is_file():
            raise RuntimeError("real 缺少 wrfbdy_d01")
        self.mark("real_prepare")

    def _validate_wrf_stage_outputs(self, directory: Path, target: Dict[str, Any]) -> None:
        """Validate every hourly history/restart artifact before a stage marker is written."""
        for value in self.times:
            history = directory / f"wrfout_d01_{stamp(value)}"
            if not history.is_file() or history.stat().st_size <= 0:
                raise RuntimeError(f"WRF 输出缺少历史场: {history.name}")
            self._validate_netcdf(history, target)
        # The initial state is wrfinput_d01. WRF writes the first restart
        # after one output interval, so it must not be required at the start.
        for value in self.times[1:]:
            restart = directory / f"wrfrst_d01_{stamp(value)}"
            if not restart.is_file() or restart.stat().st_size <= 0:
                raise RuntimeError(f"WRF 输出缺少重启场: {restart.name}")

    @staticmethod
    def _fraction(seconds: float) -> Tuple[int, int, int]:
        denominator = 1_000_000
        whole = int(seconds)
        numerator = int(round((seconds - whole) * denominator))
        if numerator == denominator:
            return whole + 1, 0, denominator
        return whole, numerator, denominator

    def _high_time_step(self, domain_index: int) -> Tuple[int, int, int]:
        # Conservative starting steps for d05-d08; adaptive WRF may reduce
        # them further to keep target_cfl below the recorded threshold.
        return self._fraction({5: 0.5, 6: 0.25, 7: 0.125, 8: 0.0625}[domain_index])

    @classmethod
    def _adaptive_time_step_bound(cls, seconds: float) -> Tuple[int, int]:
        whole, numerator, denominator = cls._fraction(seconds)
        # Adaptive bounds have one integer numerator plus a denominator; they
        # do not have the separate fractional numerator used by time_step.
        return whole * denominator + numerator, denominator

    def _ndown_namelist(self, parent: Dict[str, Any], child: Dict[str, Any], parent_domain: int) -> str:
        parent_is_les = parent_domain >= 5
        return f"""&time_control
{self._common_time(2)}
 interval_seconds = 3600, input_from_file = .true., .true.,
 history_interval = 60, 60, restart = .false., restart_interval = 60,
 io_form_history = 2, io_form_restart = 2, io_form_input = 2, io_form_boundary = 2, io_form_auxinput2 = 2,
/
&domains
 time_step = 15, max_dom = 2,
 e_we = {int(parent['e_we'])}, {int(child['e_we'])}, e_sn = {int(parent['e_sn'])}, {int(child['e_sn'])},
 e_vert = {int(self.wrf.get('e_vert', 88))}, {int(self.wrf.get('e_vert', 88))},
 num_metgrid_levels = 34, num_metgrid_soil_levels = 4,
 dx = {float(parent['dx_m'])}, {float(child['dx_m'])}, dy = {float(parent['dy_m'])}, {float(child['dy_m'])},
 grid_id = 1, 2, parent_id = 0, 1, i_parent_start = 1, {int(child['parent_start_i'])},
 j_parent_start = 1, {int(child['parent_start_j'])}, parent_grid_ratio = 1, 3, parent_time_step_ratio = 1, 3,
 feedback = 0, smooth_option = 0,
/
&physics
{self._physics([parent_is_les, True])}
/
&dynamics
{self._dynamics([parent_is_les, True])}
/
&bdy_control
 spec_bdy_width = 5, specified = .true., .false., nested = .false., .true.,
/
&namelist_quilt
 nio_tasks_per_group = 0, nio_groups = 1,
/
"""

    def _wrf_namelist(self, target: Dict[str, Any], domain_index: int, restart_time: Optional[datetime] = None) -> str:
        integer, numerator, denominator = self._high_time_step(domain_index)
        requested_seconds = integer + numerator / denominator
        starting_bound, starting_denominator = self._adaptive_time_step_bound(requested_seconds)
        min_bound, min_denominator = self._adaptive_time_step_bound(requested_seconds / 4.0)
        max_bound, max_denominator = self._adaptive_time_step_bound(requested_seconds)
        adaptive = domain_index < 8
        return f"""&time_control
{self._common_time(1, restart_time)}
 interval_seconds = 3600, input_from_file = .true., history_interval = 60,
 restart = {'.true.' if restart_time else '.false.'}, restart_interval = 60,
 io_form_history = 2, io_form_restart = 2, io_form_input = 2, io_form_boundary = 2,
 frames_per_outfile = 1,
 iofields_filename = "wrf_les_iofields.txt", ignore_iofields_warning = .false.,
/
&domains
 time_step = {integer}, time_step_fract_num = {numerator}, time_step_fract_den = {denominator},
 use_adaptive_time_step = {'.true.' if adaptive else '.false.'}, step_to_output_time = {'.true.' if adaptive else '.false.'}, target_cfl = 1.2, target_hcfl = 0.84,
 starting_time_step = {starting_bound}, starting_time_step_den = {starting_denominator},
 min_time_step = {min_bound}, min_time_step_den = {min_denominator},
 max_time_step = {max_bound}, max_time_step_den = {max_denominator},
 max_dom = 1, e_we = {int(target['e_we'])}, e_sn = {int(target['e_sn'])}, e_vert = {self.wrf.get('e_vert', 88)},
 num_metgrid_levels = 34, num_metgrid_soil_levels = 4, dx = {float(target['dx_m'])}, dy = {float(target['dy_m'])},
 grid_id = 1, parent_id = 0, parent_grid_ratio = 1, parent_time_step_ratio = 1, feedback = 0, smooth_option = 0,
/
&physics
{self._physics([True])}
/
&dynamics
{self._dynamics([True])}
/
&bdy_control
 spec_bdy_width = 5, specified = .true., nested = .false.,
/
&namelist_quilt
 nio_tasks_per_group = 0, nio_groups = 1,
/
"""

    def _write_wrf_iofields(self, directory: Path) -> None:
        """Add the 3-D turbulence diagnostics to the main history stream.

        TKE is a valid Registry state name in the current WRF build, but it
        is not part of its default history stream for this profile.  The
        build does not expose QKE as a state variable for runtime I/O.
        Keep this file next to namelist.input so the output contract cannot
        silently degrade when the WRF executable or Registry is replaced.
        """
        self._write(directory / "wrf_les_iofields.txt", WRF_LES_IOFIELDS_TEXT)

    def _latest_restart(self, directory: Path) -> Optional[Tuple[datetime, Path]]:
        # Never chain a second recovery from a checkpoint that already failed
        # after being restored. Also reject checkpoints produced by the old
        # adaptive d08 policy whose output-boundary jump caused the crash.
        if (directory / "resume_from.json").exists():
            return None
        try:
            namelist = (directory / "namelist.input").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if "use_adaptive_time_step = .false." not in namelist or "step_to_output_time = .false." not in namelist:
            return None
        candidates: List[Tuple[datetime, Path]] = []
        for path in directory.glob("wrfrst_d01_????-??-??_??:??:??"):
            try:
                value = datetime.strptime(path.name[-19:], "%Y-%m-%d_%H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                continue
            if self.start < value < self.end and path.is_file() and path.stat().st_size > 0:
                candidates.append((value, path))
        return max(candidates, default=None, key=lambda item: item[0])

    def _parent_links(self, parent_dir: Path, parent_domain: int, ndown_dir: Path) -> None:
        pattern = f"wrfout_d{parent_domain:02d}_*" if parent_domain == 4 else "wrfout_d01_*"
        for source in sorted(parent_dir.glob(pattern)):
            name = source.name if parent_domain != 4 else source.name.replace("wrfout_d04_", "wrfout_d01_", 1)
            self._link(source, ndown_dir / name)

    def _ndown_and_wrf(self, parent_domain: int, child_domain: int, parent_dir: Path) -> Path:
        child = self.domains[child_domain - 1]
        parent = self.domains[parent_domain - 1]
        ndown_name = f"ndown_d{parent_domain:02d}_d{child_domain:02d}"
        ndown_dir = self.root / ndown_name
        child_dir = self.root / f"wrf_d{child_domain:02d}"
        wrf_name = {5: "wrf_d05_333m", 6: "wrf_d06_111m", 7: "wrf_d07_37m", 8: "wrf_d08_10m"}[child_domain]
        if not self.done(ndown_name):
            self._archive_incomplete(ndown_dir, ndown_name)
        restart_checkpoint = self._latest_restart(child_dir) if child_dir.is_dir() else None
        restart_archive = None
        if not self.done(wrf_name):
            restart_archive = self._archive_incomplete(child_dir, f"wrf_d{child_domain:02d}")
        ndown_dir.mkdir(parents=True, exist_ok=True)
        child_dir.mkdir(parents=True, exist_ok=True)
        if not self.done(ndown_name):
            self._link_wrf_runtime_files(ndown_dir)
            self._parent_links(parent_dir, parent_domain, ndown_dir)
            self._copy(self.real / f"wrfinput_d{child_domain:02d}", ndown_dir / "wrfndi_d02")
            self._copy(self.real / f"wrfinput_d{child_domain:02d}", ndown_dir / "wrfinput_d02")
            self._link(self.wrf_source / "main/ndown.exe", ndown_dir / "ndown.exe")
            self._write(ndown_dir / "namelist.input", self._ndown_namelist(parent, child, parent_domain))
            self._run(ndown_dir, "ndown", ["mpirun", "-np", str(self.mpi), "./ndown.exe"])
            for filename in ("wrfinput_d02", "wrfbdy_d02"):
                path = ndown_dir / filename
                if not path.is_file() or path.stat().st_size <= 0:
                    raise RuntimeError(f"{ndown_name} 未形成 {filename}")
            self.mark(ndown_name)

        if not self.done(wrf_name):
            self._link_wrf_runtime_files(child_dir)
            self._copy(ndown_dir / "wrfinput_d02", child_dir / "wrfinput_d01")
            self._copy(ndown_dir / "wrfbdy_d02", child_dir / "wrfbdy_d01")
            self._link(self.wrf_source / "main/wrf.exe", child_dir / "wrf.exe")
            self._write_wrf_iofields(child_dir)
            restart_time = None
            if restart_checkpoint and restart_archive:
                restart_time, original = restart_checkpoint
                archived = restart_archive / original.name
                if archived.is_file() and archived.stat().st_size > 0:
                    self._copy(archived, child_dir / archived.name)
                    initial_history = restart_archive / f"wrfout_d01_{stamp(self.start)}"
                    if initial_history.is_file() and initial_history.stat().st_size > 0:
                        self._copy(initial_history, child_dir / initial_history.name)
                    self._write(child_dir / "resume_from.json", json.dumps({
                        "schema_version": "wrf-les-resume/v1", "restart_time": iso(restart_time),
                        "source": str(archived),
                    }, ensure_ascii=False, indent=2) + "\n")
                else:
                    restart_time = None
            self._write(child_dir / "namelist.input", self._wrf_namelist(child, child_domain, restart_time))
            self._run(child_dir, "wrf", ["mpirun", "-np", str(self.mpi), "./wrf.exe"])
            self._validate_wrf_stage_outputs(child_dir, child)
            self.mark(wrf_name)
        return child_dir

    def _validate_netcdf(self, path: Path, target: Dict[str, Any]) -> List[str]:
        command = ["ncdump", "-h", str(path)]
        if self.runtime_env.is_file():
            shell = f". {shlex.quote(str(self.runtime_env))} >/dev/null 2>&1 && exec {self._shell_join(command)}"
            result = subprocess.run(["bash", "-lc", shell], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False)
        else:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"最终 forcing NetCDF 无法打开: {path.name}: {result.stderr.strip()[:300]}")
        header = result.stdout
        expected_dx = float(target["dx_m"])
        dx_match = re.search(r":DX\s*=\s*([0-9.]+)f?\s*;", header)
        dy_match = re.search(r":DY\s*=\s*([0-9.]+)f?\s*;", header)
        if not dx_match or abs(float(dx_match.group(1)) - expected_dx) > 0.02 or not dy_match or abs(float(dy_match.group(1)) - expected_dx) > 0.02:
            raise RuntimeError(f"{path.name} 网格间距不符合 d08 合同")
        bottom_match = re.search(r":BOTTOM-TOP_GRID_DIMENSION\s*=\s*(\d+)\s*;", header)
        if not bottom_match or int(bottom_match.group(1)) < int(self.wrf.get("e_vert", 88)):
            raise RuntimeError(f"{path.name} 垂直层数不足")
        variables = []
        for line in header.splitlines():
            match = re.match(r"\s+(?:float|double|int|short|byte|char)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
            if match:
                variables.append(match.group(1).upper())
        self._validate_vertical_coordinates(path)
        self._validate_finite_values(path, variables)
        return sorted(set(variables))

    def _validate_finite_values(self, path: Path, variables: List[str]) -> None:
        """Stream the required numeric fields through ncdump without buffering them."""
        required = [
            "U", "V", "W", "T", "P", "PB", "PH", "PHB", "QVAPOR", "QKE", "TKE",
            "HGT", "UST", "HFX", "PBLH",
        ]
        selected = [name for name in required if name in variables]
        if not selected:
            return
        command = ["ncdump", "-v", ",".join(selected), str(path)]
        if self.runtime_env.is_file():
            shell = f". {shlex.quote(str(self.runtime_env))} >/dev/null 2>&1 && exec {self._shell_join(command)}"
            wrapped = ["bash", "-lc", shell]
        else:
            wrapped = command
        process = subprocess.Popen(wrapped, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        bad_line = None
        assert process.stdout is not None
        for line in process.stdout:
            if re.search(r"(?:NaN|[+-]?Inf)(?:f)?\b", line, re.I):
                bad_line = line.strip()[:300]
                process.kill()
                break
        return_code = process.wait()
        if bad_line is not None:
            raise RuntimeError(f"{path.name} 数值变量包含非有限值：{bad_line}")
        if return_code != 0:
            raise RuntimeError(f"{path.name} 数值变量有限值检查失败，ncdump 退出码 {return_code}")

    def _validate_vertical_coordinates(self, path: Path) -> None:
        """Check compact eta-coordinate arrays without dumping 3-D fields."""
        command = ["ncdump", "-v", "ZNU,ZNW", str(path)]
        if self.runtime_env.is_file():
            shell = f". {shlex.quote(str(self.runtime_env))} >/dev/null 2>&1 && exec {self._shell_join(command)}"
            result = subprocess.run(["bash", "-lc", shell], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False)
        else:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{path.name} 垂直坐标 ZNU/ZNW 无法读取")
        output = result.stdout
        if re.search(r"\b(?:NaN|Inf|-Inf)\b", output, re.I):
            raise RuntimeError(f"{path.name} 垂直坐标包含非有限值")
        for variable in ("ZNU", "ZNW"):
            match = re.search(r"\b" + variable + r"\s*=\s*(.*?)\s*;", output, re.S)
            if not match:
                raise RuntimeError(f"{path.name} 缺少垂直坐标 {variable}")
            values = [float(token) for token in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?", match.group(1))]
            monotonic = all(left > right for left, right in zip(values, values[1:])) or all(left < right for left, right in zip(values, values[1:]))
            if len(values) < 2 or not monotonic:
                raise RuntimeError(f"{path.name} 的 {variable} 垂直坐标不单调")

    def _publish_forcing(self, final_dir: Path) -> None:
        target = self.domains[-1]
        forcing_dir = self.output_dir / "forcing"
        forcing_dir.mkdir(parents=True, exist_ok=True)
        entries: List[Dict[str, Any]] = []
        actual_variables = set()
        for value in self.times:
            source = final_dir / f"wrfout_d01_{stamp(value)}"
            if not source.is_file() or source.stat().st_size <= 0:
                raise RuntimeError(f"最终 d08 forcing 缺少时次: {iso(value)}")
            variables = self._validate_netcdf(source, target)
            actual_variables.update(variables)
            destination = forcing_dir / f"wrfout_d08_{stamp(value)}.nc"
            self._copy(source, destination)
            stat = destination.stat()
            entries.append({
                "valid_time_utc": iso(value),
                "path": str(destination.relative_to(self.output_dir)),
                "size_bytes": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z"),
                "sha256": sha256_file(destination),
                "variables": variables,
            })
        if len(entries) != len(self.times) or [item["valid_time_utc"] for item in entries] != self.valid_times:
            raise RuntimeError("最终 d08 forcing 时次不连续或存在重复")
        validate_forcing_inventory(self.output_dir, entries, self.valid_times)
        required = {str(value).upper() for value in ((self.contract.get("forcing_contract") or {}).get("required_variables") or [])}
        missing = required - actual_variables
        if {"QKE", "TKE"}.issubset(missing) or ("QKE" in missing and "TKE" not in actual_variables) or ("TKE" in missing and "QKE" not in actual_variables):
            raise RuntimeError("最终 d08 forcing 缺少三维湍流诊断变量 QKE/TKE")
        missing -= {"QKE", "TKE"}
        if missing:
            raise RuntimeError("最终 d08 forcing 缺少合同要求的输出变量: " + ", ".join(sorted(missing)))
        task_id = self._task_id()
        step_values = {f"d{index:02d}": self._high_time_step(index)[0] + self._high_time_step(index)[1] / self._high_time_step(index)[2] for index in range(5, 9)}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "topology_id": TOPOLOGY_ID,
            "task_id": task_id,
            "nominal_resolution_m": 10,
            "horizontal_spacing_m": self.target_dx,
            "domain_count": len(self.domains),
            "domains": self.domains,
            "time_range": {"start_utc": iso(self.start), "end_utc": iso(self.end), "duration_hours": int((self.end - self.start).total_seconds() // 3600), "valid_time_count": len(entries), "cadence": "PT1H"},
            "valid_times_utc": self.valid_times,
            "final_domain": "d08",
            "forcing_files": entries,
            "coverage": {
                "aoi": self.contract.get("publication_aoi") or {},
                "final_extent_m": {
                    "x": (float(target["e_we"]) - 1.0) * float(target["dx_m"]),
                    "y": (float(target["e_sn"]) - 1.0) * float(target["dy_m"]),
                },
                "halo_each_side_m": 500.0,
                "coordinate_reference": "d08 local WRF grid; AOI anchor is carried by publication_aoi",
            },
            "horizontal_crs": (self.contract.get("publication_aoi") or {}).get("horizontal_crs"),
            "vertical": {"layer_count": int(self.wrf.get("e_vert", 88)), "top_pressure_pa": 5000, "top_height_m": 20000, "top_height_reference": "nominal model top corresponding to p_top_requested=5000 Pa", "coordinate_validation": "WRF NetCDF header and ZNU/ZNW monotonicity checked"},
            "vertical_layer_count": int(self.wrf.get("e_vert", 88)),
            "variables": sorted(actual_variables),
            "physics": {
                "normal_domains": {"d01-d04": "request physics"},
                "wrf_les_profile_id": LES_PROFILE_ID,
                "d05-d08": {"bl_pbl_physics": 0, "diff_opt": 2, "km_opt": 2, "cu_physics": 0, "e_vert": int(self.wrf.get("e_vert", 88)), "history_interval_minutes": 60, "restart_interval_minutes": 60},
            },
            "time_step_control": {"target_cfl": 1.2, "target_hcfl": 0.84, "actual_max_cfl": self.max_cfl, "actual_max_cfl_available": self.max_cfl is not None, "actual_max_cfl_source": "wrf_log" if self.max_cfl is not None else "not_emitted_by_current_wrf_build", "adaptive": True, "initial_time_step_seconds": step_values, "minimum_time_step_seconds": {key: value / 4 for key, value in step_values.items()}, "maximum_time_step_seconds": step_values, "fractional_steps_seconds": step_values, "auto_shrink_observed": self.time_step_reduced},
            "wrf_build": {"wrf_version": env("WRF_VERSION", "WRF build identified by binary hashes"), "binary_sha256": self.binary_hashes, "runtime_env": str(self.runtime_env)},
            "source_wrf_task_id": self.source_task,
            "upstream_d04": {"final_domain": self.source_domain, "source_output_dir": str(self.source_run), "inventory_sha256": self.source_artifact.get("inventory_sha256"), "files": self.source_artifact.get("files") or []},
            "quality": {"status": "pass", "logs_scanned": True, "netcdf_open_checked": True, "finite_values_check": "required numeric WRF fields streamed through ncdump; ZNU/ZNW coordinates and WRF rank logs checked"},
            "quality_status": "mesoscale_forcing_complete",
            "continuous_1m_time_series": False,
            "publication": {"mode": "internal_forcing_asset", "cfd_started": False},
            "parameterization": self.contract.get("parameterization"),
            "configuration_hash": self.contract.get("configuration_hash"),
        }
        self._write(self.output_dir / "mesoscale_forcing_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    def execute(self) -> None:
        if not self.done("source_handoff"):
            self.write_stage("source_handoff", "ndown_d04_d05", "普通 WRF d04 原生历史场和重启场已通过稳定性检查")
            self.mark("source_handoff")
        self.wps_prepare()
        self.real_prepare()
        parent_dir = self.source_run
        parent_domain = 4
        if not self.done("forcing_ready"):
            for child_domain in range(5, 9):
                ndown_stage = f"ndown_d{parent_domain:02d}_d{child_domain:02d}"
                wrf_stage = {5: "wrf_d05_333m", 6: "wrf_d06_111m", 7: "wrf_d07_37m", 8: "wrf_d08_10m"}[child_domain]
                next_after_wrf = f"ndown_d{child_domain:02d}_d{child_domain + 1:02d}" if child_domain < 8 else "forcing_ready"
                self.write_stage(ndown_stage, wrf_stage, f"执行 d{parent_domain:02d} → d{child_domain:02d} NDOWN")
                child_dir = self._ndown_and_wrf(parent_domain, child_domain, parent_dir)
                self.write_stage(wrf_stage, next_after_wrf, f"d{child_domain:02d} WRF-LES 完成，继续生成下一层 forcing")
                parent_dir = child_dir
                parent_domain = child_domain
            self._publish_forcing(parent_dir)
            self.mark("forcing_ready")
        if self.composite_cfd:
            self.write_stage("forcing_ready", "source_forcing_handoff", f"d08 · {self.target_dx:.6f} m forcing 已完成，自动进入同一任务的 CFD 阶段")
            cfd_script = Path(__file__).with_name("wrf_hpc_cfd_10m_chain.py")
            subprocess.run([sys.executable, str(cfd_script), "--contract", str(self.contract_path), "--output", str(self.output_dir)], check=True)
            return
        self.write_stage("forcing_ready", "publish", f"d08 · {self.target_dx:.6f} m forcing 已完成，{len(self.times)} 个逐小时时次，任务不启动 CFD")
        print(f"WRF_LES_FORCING_READY|source={self.source_task}|topology={TOPOLOGY_ID}|target_dx={self.target_dx:.6f}|valid_times={len(self.times)}|output={self.output_dir}")

    def write_stage(self, status: str, next_stage: str, message: str) -> None:
        task_id = self._task_id()
        payload = {
            "schema_version": "wrf-les-forcing-stage/v2",
            "task_id": task_id,
            "status": status,
            "next_stage": next_stage,
            "message": message,
            "source_wrf_task_id": self.source_task,
            "source_domain": self.source_domain,
            "topology_id": TOPOLOGY_ID,
            "nominal_resolution_m": 10,
            "target_dx_m": self.target_dx,
            "contract_sha256": self.contract_sha256,
            "topology_sha256": self.topology_sha256,
            "input_inventory_sha256": self._input_hash(),
            "wrf_binary_sha256": self.binary_hashes,
            "stage_config_sha256": self.stage_config_sha256,
            "run_dir": str(self.root),
            "ready_for_ndown": status == "source_handoff",
            "cfd_not_started": True,
        }
        self._write(self.output_dir / "high-resolution-stage.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    try:
        contract = Path(env("WRF_DOWNSCALE_CONTRACT"))
        task_dir = Path(env("WRF_DOWNSCALE_TASK_DIR"))
        output_dir = Path(env("WRF_DOWNSCALE_OUTPUT_DIR"))
        if not contract.is_file() or not task_dir.is_dir() or not output_dir.is_dir():
            raise ValueError("高分链环境变量缺少有效合同或任务目录")
        Chain(contract, task_dir, output_dir).execute()
        return 0
    except Exception as exc:
        print(f"[高分链失败] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
