"""Decode tiny real GRIBs before isolated task preparation; never launch WRF.

Run explicitly with the ecCodes native test dependencies on PYTHONPATH.
All files and SQLite records belong to TemporaryDirectory fixtures.
"""

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import eccodes as ec
import numpy as np

from config import settings
from database import TaskStore
from gfs import GfsManager
from guidance import recommendations
from hpc_transport import HpcClient, HpcError
from schemas import WrfTaskCreate
from scripts.validate_forcing import validate
from task_manager import WrfTaskManager

CYCLE = "2026102800"
TASK = "wrf_gfs_20261029T000000Z_abcdef01"
ROOT = Path(__file__).resolve().parent


def write_grib(path, hour, *, cycle=CYCLE, spacing=0.25):
    """Encode real pressure, surface and four matched soil-layer fields."""
    pressure = [1000, 925, 850, 700, 600, 500, 400, 300, 200, 50]
    layers = [(0, 10), (10, 40), (40, 100), (100, 200)]
    fields = [
        (name, level, None)
        for name in ("t", "u", "v", "r", "gh")
        for level in pressure
    ]
    fields += [("sp", None, None), ("2t", None, None)]
    fields += [
        (name, None, layer) for name in ("stl1", "swvl1") for layer in layers
    ]
    size = round(6 / spacing) + 1
    with path.open("wb") as stream:
        for name, level, layer in fields:
            sample = "regular_ll_pl_grib2" if level else "regular_ll_sfc_grib2"
            handle = ec.codes_grib_new_from_samples(sample)
            try:
                for key, value in {
                    "Ni": size,
                    "Nj": size,
                    "latitudeOfFirstGridPointInDegrees": 38,
                    "latitudeOfLastGridPointInDegrees": 32,
                    "longitudeOfFirstGridPointInDegrees": 110,
                    "longitudeOfLastGridPointInDegrees": 116,
                    "iDirectionIncrementInDegrees": spacing,
                    "jDirectionIncrementInDegrees": spacing,
                    "dataDate": int(cycle[:8]),
                    "dataTime": 0,
                    "step": hour,
                }.items():
                    ec.codes_set(handle, key, value)
                if level:
                    ec.codes_set(handle, "level", level)
                ec.codes_set(handle, "shortName", name)
                if layer:
                    ec.codes_set(handle, "typeOfLevel", "depthBelowLandLayer")
                    for key, value in {
                        "scaleFactorOfFirstFixedSurface": 2,
                        "scaledValueOfFirstFixedSurface": layer[0],
                        "scaleFactorOfSecondFixedSurface": 2,
                        "scaledValueOfSecondFixedSurface": layer[1],
                    }.items():
                        ec.codes_set(handle, key, value)
                ec.codes_set_values(handle, np.full(size * size, 1.0))
                ec.codes_write(handle, stream)
            finally:
                ec.codes_release(handle)


class StopBeforeUpload(RuntimeError):
    """The positive test must stop before upload or any remote execution."""


class NativeForcingIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="wrf-native-forcing-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cfg = replace(
            settings,
            database_path=self.root / "tasks.sqlite3",
            run_dir=self.root / "runs",
            data_dir=self.root / "data",
            hpc_remote_dir=str(self.root / "runtime"),
            hpc_gfs_dir=str(self.root / "forcing" / "gfs"),
            hpc_ec_dir=str(self.root / "forcing" / "ecmwf"),
            hpc_ec_0p1_dir=str(self.root / "forcing" / "ecmwf_0p1"),
            hpc_runtime_release=None,
            hpc_runtime_python=sys.executable,
            hpc_gfs_full_min_bytes=1,
            hpc_ec_full_min_bytes=1,
            runtime_asset_root=None,
            legacy_asset_manifest=None,
            hpc_gfs_prefetch_enabled=False,
        )
        self.store = TaskStore(self.cfg.database_path)
        self.addCleanup(self.store.close)
        self.hpc = HpcClient(self.cfg)
        self.commands = []
        self.hpc.run = self.local_read
        self.service = Path(self.hpc.service_dir())
        self.service.mkdir(parents=True)
        shutil.copy2(ROOT / "scripts" / "validate_forcing.py", self.service)
        (self.service.parent / "runtime.env").write_text(
            "# Isolated test only\n"
        )
        self.cycle = Path(self.cfg.hpc_gfs_dir) / CYCLE
        self.cycle.mkdir(parents=True)
        self.paths = []
        for hour in (24, 25):
            path = self.cycle / f"gfs.t00z.pgrb2.0p25.f{hour:03d}"
            write_grib(path, hour)
            self.paths.append(path)
        self.request = WrfTaskCreate.model_validate(
            {
                "configuration_profile": "wrf-ordinary-ui-2",
                "data_source": "gfs",
                "start_time": "2026-10-29T00:00:00Z",
                "end_time": "2026-10-29T01:00:00Z",
                "forecast_interval_hours": 1,
                "center": {"lat": 35, "lon": 113},
                "domains": [
                    {
                        "id": "d01",
                        "dx": 27000,
                        "dy": 27000,
                        "e_we": 11,
                        "e_sn": 11,
                        "parent_id": 0,
                    }
                ],
                "spinup": {"mode": "custom", "hours": 0},
                "publish_to_catalog": False,
            }
        ).model_dump(mode="json")
        self.store.create(TASK, self.request, "isolated-owner")
        self.network = patch(
            "socket.socket.connect",
            side_effect=AssertionError(
                "Network is forbidden in native forcing regression"
            ),
        )
        self.network.start()
        self.addCleanup(self.network.stop)

    def local_read(self, command, timeout=None):
        self.assertIn(str(self.root), command)
        self.assertFalse(
            any(
                word in command
                for word in (
                    "curl ",
                    "ssh ",
                    "scp ",
                    "mpirun",
                    "wrf.exe",
                    "rm ",
                )
            )
        )
        self.assertTrue(command.startswith(("if test -s ", "cd ", ". ")))
        self.commands.append(command)
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=min(timeout or 30, 30),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if result.returncode:
            raise HpcError("Isolated decoder stopped: " + result.stderr[-600:])
        return result.stdout

    def manager(self):
        # Keep transport/release provisioning outside this file-read test;
        # use the production inspector, decoder, selector and manager body.
        def ensure_local(source, cycle, hours, **kwargs):
            return self.hpc.inspect_forcing_files(source, cycle, hours)

        proxy = SimpleNamespace(
            complete_forcing_cycles=lambda source: [CYCLE],
            ensure_remote_forcing=ensure_local,
            validate_forcing_semantics=self.hpc.validate_forcing_semantics,
            prepare_runtime=Mock(side_effect=StopBeforeUpload("No upload")),
            launch=Mock(side_effect=AssertionError("No WRF launch")),
        )
        manager = WrfTaskManager(
            self.cfg, self.store, GfsManager(self.cfg), proxy
        )
        return manager, proxy

    def advisory(self, *, missing=False):
        return recommendations(
            {**self.request, "duration_hours": 1},
            {},
            {
                "status": "ok",
                "pool_items": [
                    {
                        "provider": "gfs",
                        "cycles": [
                            {
                                "cycle": CYCLE,
                                "complete": True,
                                "available_hours": [24]
                                if missing
                                else [24, 25],
                            }
                        ],
                    }
                ],
            },
            datetime(2026, 10, 29, 0, 17, tzinfo=timezone.utc),
        )["data_window"]

    def test_real_fields_pass_decoding_and_stop_before_upload(self):
        window = self.advisory()
        self.assertEqual(window["cycle"], CYCLE)
        self.assertEqual(window["status"], "unknown")
        self.assertEqual(window["availability_status"], "present")
        result = validate(self.paths[0], "202610290000", [33, 111, 37, 115])
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["messages"], 60)
        manager, proxy = self.manager()
        with self.assertRaises(StopBeforeUpload):
            manager._execute(TASK)
        proxy.launch.assert_not_called()
        runtime = self.store.get(TASK)["runtime"]
        self.assertEqual(runtime["forcing_cycle"], CYCLE)
        self.assertEqual(runtime["forecast_hours"], [24, 25])
        self.assertTrue(runtime["forcing_semantic_validation"]["valid"])

    def test_trailing_non_grib_bytes_cannot_reach_upload(self):
        with self.paths[1].open("ab") as stream:
            stream.write(b"unparsed trailing bytes7777")
        manager, proxy = self.manager()
        with self.assertRaisesRegex(HpcError, "语义校验"):
            manager._execute(TASK)
        proxy.prepare_runtime.assert_not_called()
        proxy.launch.assert_not_called()
        self.assertNotIn(
            "forcing_semantic_validation", self.store.get(TASK)["runtime"]
        )

    def test_incomplete_decoder_reports_never_count_as_verified_files(self):
        good = self.hpc.validate_forcing_semantics(
            TASK, "gfs", CYCLE, [24, 25], [33, 111, 37, 115]
        )
        cases = [
            {**good, "files": []},
            {**good, "files": good["files"][:1]},
            {**good, "valid": "true"},
            {**good, "files": [good["files"][0], good["files"][0]]},
            {**good, "valid": False, "files": [{"issues": None}]},
            {**good, "files": None},
        ]
        for field, value in (
            ("name", "another.grib2"),
            ("valid", "true"),
            ("messages", 0),
            ("valid_times", ["202610300000"]),
            ("decoded_bytes", 0),
            ("file_bytes", "10740"),
        ):
            cases.append(
                {
                    **good,
                    "files": [
                        {**good["files"][0], field: value},
                        good["files"][1],
                    ],
                }
            )
        for report in cases:
            with self.subTest(report=report), patch.object(
                self.hpc, "run", return_value=json.dumps(report)
            ):
                with self.assertRaises(HpcError):
                    self.hpc.validate_forcing_semantics(
                        TASK, "gfs", CYCLE, [24, 25], [33, 111, 37, 115]
                    )

    def test_same_size_change_is_rejected_by_original_shell_checksum_gate(
        self,
    ):
        index = self.root / "expected.tsv"
        rows = []
        for path in self.paths:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(f"{path.name}\t{path.stat().st_size}\t{digest}\n")
        index.write_text("".join(rows))
        env = {
            **os.environ,
            "WRF_NONINTERACTIVE": "true",
            "WORK_DIR": str(self.root),
            "WRF_ATTEMPT_DIR": str(self.root),
            "WRF_TASK_TAG": "native-fixture",
            "WRF_GFS_EXPECTED_INDEX": str(index),
            "WRF_GFS_DATA_ROOT": self.cfg.hpc_gfs_dir,
            "WRF_GFS_DATE": CYCLE[:8],
            "WRF_GFS_HOUR": "00",
            "WRF_GFS_FORECAST_HOURS": "024 025",
        }
        command = [
            "bash",
            "-c",
            'source "$1"; step1_forecast_data_preparation gfs',
            "fixture",
            str(ROOT / "scripts" / "wrf.sh"),
        ]
        good = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=10
        )
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        raw = bytearray(self.paths[1].read_bytes())
        raw[-10] ^= 1
        self.paths[1].write_bytes(raw)
        bad = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=10
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("SHA256 不一致", bad.stdout + bad.stderr)

    def test_missing_and_wrong_time_files_stop_before_upload(self):
        manager, proxy = self.manager()
        self.paths[1].unlink()
        self.assertNotEqual(self.advisory(missing=True)["status"], "ready")
        with self.assertRaises(HpcError):
            manager._execute(TASK)
        proxy.prepare_runtime.assert_not_called()
        proxy.launch.assert_not_called()
        shutil.copy2(self.paths[0], self.paths[1])
        manager, proxy = self.manager()
        with self.assertRaisesRegex(HpcError, "missing_valid_time"):
            manager._execute(TASK)
        proxy.prepare_runtime.assert_not_called()
        proxy.launch.assert_not_called()

    def test_replacing_file_during_decode_invalidates_the_observation(self):
        original = ec.codes_grib_new_from_file
        replacement = self.paths[0].with_suffix(".replacement")
        shutil.copy2(self.paths[0], replacement)
        changed = False

        def read(stream):
            nonlocal changed
            handle = original(stream)
            if not changed:
                replacement.replace(self.paths[0])
                changed = True
            return handle

        with patch.object(ec, "codes_grib_new_from_file", side_effect=read):
            result = validate(
                self.paths[0], "202610290000", [33, 111, 37, 115]
            )
        self.assertFalse(result["valid"])
        self.assertIn("forcing_file_changed", result["issues"])

    def test_real_grid_spacing_distinguishes_tenth_from_quarter_degree(self):
        path = self.root / "ifs-tenth.grib2"
        write_grib(path, 24, spacing=0.1)
        good = validate(
            path, "202610290000", [33, 111, 37, 115], expected_spacing=0.1
        )
        self.assertTrue(good["valid"], good)
        bad = validate(
            self.paths[0],
            "202610290000",
            [33, 111, 37, 115],
            expected_spacing=0.1,
        )
        self.assertFalse(bad["valid"])
        self.assertIn("grid_spacing_mismatch", bad["issues"])

    def test_empty_unknown_and_duplicate_hours_do_not_run_the_decoder(self):
        for hours in ([], None, [True], [24, 24], [73]):
            with self.assertRaises(ValueError):
                self.hpc.validate_forcing_semantics(
                    TASK, "gfs", CYCLE, hours, [33, 111, 37, 115]
                )
        self.assertEqual(self.commands, [])

    def test_legacy_reports_without_optional_byte_counts_stay_compatible(self):
        report = self.hpc.validate_forcing_semantics(
            TASK, "gfs", CYCLE, [24, 25], [33, 111, 37, 115]
        )
        for file in report["files"]:
            file.pop("decoded_bytes")
            file.pop("file_bytes")
        with patch.object(self.hpc, "run", return_value=json.dumps(report)):
            self.assertTrue(
                self.hpc.validate_forcing_semantics(
                    TASK, "gfs", CYCLE, [24, 25], [33, 111, 37, 115]
                )["valid"]
            )

    def test_broken_packed_values_cannot_reach_upload(self):
        messages = []
        with self.paths[1].open("rb") as stream:
            while True:
                handle = ec.codes_grib_new_from_file(stream)
                if handle is None:
                    break
                try:
                    ec.codes_set(handle, "bitsPerValue", 16)
                    ec.codes_set_values(handle, np.arange(625, dtype=float))
                    messages.append(ec.codes_get_message(handle))
                finally:
                    ec.codes_release(handle)
        last = bytearray(messages[-1])
        offset = 16
        while last[offset + 4] != 7:
            end = offset + 4
            offset += int.from_bytes(last[offset:end], "big")
        # Keep a valid GRIB envelope but truncate the packed field payload.
        broken = last[:offset] + (6).to_bytes(4, "big") + b"\x07\x00" + b"7777"
        broken[8:16] = len(broken).to_bytes(8, "big")
        self.paths[1].write_bytes(b"".join(messages[:-1]) + broken)
        manager, proxy = self.manager()
        with self.assertRaises(HpcError):
            manager._execute(TASK)
        proxy.prepare_runtime.assert_not_called()
        proxy.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
