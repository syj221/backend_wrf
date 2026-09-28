import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from downscale.schemas import FORCING_DX_M, DownscaleValidationRequest, FORCING_INITIAL_TIME_STEP_SECONDS
from downscale.runner_contract import build_runner_contract


MODULE_PATH = Path(__file__).parent / "scripts" / "wrf_hpc_downscale_chain.py"
SPEC = importlib.util.spec_from_file_location("wrf_hpc_downscale_chain", MODULE_PATH)
chain_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(chain_module)


class ForcingContractTests(unittest.TestCase):
    def test_wps_driver_hours_align_with_product_start_not_spinup_start(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = chain_module.Chain.__new__(chain_module.Chain)
            chain.data_root = Path(directory)
            chain.start = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
            chain.times = [chain.start + timedelta(hours=index) for index in range(7)]
            chain.wrf = {"spinup_hours": 6}
            chain.source_artifact = {
                "source_cycle": "2026091800",
                "source_forecast_hours": list(range(6, 25)),
            }
            path, first_hour = chain._driver_cycle()
            self.assertEqual(path, Path(directory) / "2026091800")
            self.assertEqual(first_hour, 12)

            chain.source_artifact["source_forecast_hours"] = list(range(6, 13))
            with self.assertRaisesRegex(FileNotFoundError, "缺少时效"):
                chain._driver_cycle()

    def test_completed_wps_checkpoint_survives_shared_driver_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chain = chain_module.Chain.__new__(chain_module.Chain)
            chain.root = root / "chain"
            chain.wps = chain.root / "wps"
            chain.wps.mkdir(parents=True)
            chain.source_run = root / "source"
            chain.source_run.mkdir()
            chain.source_domain = "d04"
            chain.source_artifact = {"inventory_sha256": "a" * 64, "source_cycle": "2026091800", "source_forecast_hours": list(range(12, 19))}
            chain.data_root = root / "removed-driver-pool"
            chain.start = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
            chain.end = datetime(2026, 9, 18, 18, tzinfo=timezone.utc)
            chain.times = [chain.start + timedelta(hours=index) for index in range(7)]
            chain.valid_times = [value.isoformat().replace("+00:00", "Z") for value in chain.times]
            chain.wrf = {"spinup_hours": 6}
            chain.contract_sha256 = "b" * 64
            chain.topology_sha256 = "c" * 64
            chain.domains = [{"name": f"d{index:02d}"} for index in range(1, 9)]
            for index in range(1, 9):
                (chain.wps / f"geo_em.d{index:02d}.nc").write_bytes(b"geo")
                for value in chain.times:
                    (chain.wps / f"met_em.d{index:02d}.{chain_module.stamp(value)}.nc").write_bytes(b"met")
            frozen = "d" * 64
            marker = {
                "contract_sha256": chain.contract_sha256, "topology_sha256": chain.topology_sha256,
                "run_dir": str(chain.root), "input_inventory_sha256": frozen,
            }
            marker_path = chain._marker_path("wps_prepare")
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            self.assertEqual(chain._input_hash(), frozen)

    def test_wps_preserves_upstream_reference_point_and_projection(self):
        from downscale.schemas import default_wrf_domains
        chain = chain_module.Chain.__new__(chain_module.Chain)
        chain.domains = [d.model_dump() for d in default_wrf_domains()]
        chain.start_stamp = "2026-09-17_06:00:00"
        chain.end_stamp = "2026-09-17_12:00:00"
        chain.source_artifact = {"source_center": {"lat": 32.06, "lon": 118.8, "grid_x": 13.7, "grid_y": 13.2}}
        chain.contract = {"publication_aoi": {"center_lat": 32.06, "center_lon": 118.8}}
        chain.geog = "/test/geog"
        text = chain._wps_namelist()
        self.assertIn("ref_x = 13.7", text)
        self.assertIn("ref_y = 13.2", text)
        self.assertIn("truelat1 = 32.06", text)
        self.assertIn("truelat2 = 32.06", text)

    def test_default_contract_has_eight_domains_and_three_to_one_ratios(self):
        request = DownscaleValidationRequest(
            upstream_mode="full_chain",
            source_wrf_task_id="wrf_gfs_20260915T000000Z_abcdef12",
            start_time=datetime(2026, 9, 15, tzinfo=timezone.utc),
            execution_scope="forcing_only",
            aoi={"center_lat": 32, "center_lon": 118, "horizontal_crs": "EPSG:4326", "width_m": 500, "height_m": 500},
        )
        domains = request.wrf.domains
        self.assertEqual(len(domains), 8)
        self.assertAlmostEqual(domains[-1].dx_m, FORCING_DX_M, places=6)
        self.assertTrue(request.wrf.production_topology_ok())
        self.assertEqual(request.wrf.time_step_seconds, FORCING_INITIAL_TIME_STEP_SECONDS)
        coverage = request.wrf.production_coverage(500, 500)
        self.assertTrue(coverage["valid"])
        self.assertAlmostEqual(coverage["halo_each_side_m"], 500.0)
        self.assertTrue(all(item["min_parent_cells"] >= 10 for item in coverage["interfaces"]))
        self.assertFalse(request.wrf.production_coverage(600, 500)["valid"])
        for parent, child in zip(domains, domains[1:]):
            self.assertAlmostEqual(parent.dx_m / child.dx_m, 3.0, places=6)

    def test_high_domains_use_three_dimensional_tke_profile(self):
        chain = chain_module.Chain.__new__(chain_module.Chain)
        chain.wrf = {"physics": {"mp_physics": 8, "ra_lw_physics": 4, "ra_sw_physics": 4, "bl_pbl_physics": 1, "sf_sfclay_physics": 1, "sf_surface_physics": 2, "radt_minutes": 1, "cu_physics_by_domain": [0] * 8}}
        physics = chain._physics([False, False, False, False, True, True, True, True])
        dynamics = chain._dynamics([False, False, False, False, True, True, True, True])
        self.assertIn("bl_pbl_physics = 1, 1, 1, 1, 0, 0, 0, 0", physics)
        self.assertIn("cu_physics = 0, 0, 0, 0, 0, 0, 0, 0", physics)
        self.assertIn("ghg_input = 0", physics)
        self.assertIn("w_damping = 1", dynamics)
        self.assertIn("w_crit_cfl = 0.8", dynamics)
        self.assertIn("epssm = 0.0, 0.0, 0.0, 0.0, 0.1, 0.1, 0.1, 0.1", dynamics)
        self.assertIn("diff_6th_opt = 0, 0, 0, 0, 2, 2, 2, 2", dynamics)
        self.assertIn("mix_full_fields = .true., .true., .true., .true., .true., .true., .true., .true.", dynamics)
        self.assertIn("moist_adv_opt = 2, 2, 2, 2, 2, 2, 2, 2", dynamics)
        self.assertIn("scalar_adv_opt = 2, 2, 2, 2, 2, 2, 2, 2", dynamics)
        self.assertIn("diff_opt = 1, 1, 1, 1, 2, 2, 2, 2", dynamics)
        self.assertIn("km_opt = 4, 4, 4, 4, 2, 2, 2, 2", dynamics)

    def test_fractional_high_resolution_time_steps_have_fractional_bounds(self):
        chain = chain_module.Chain.__new__(chain_module.Chain)
        chain.start = datetime(2026, 9, 15, tzinfo=timezone.utc)
        chain.end = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
        chain.wrf = {"e_vert": 88}
        namelist = chain._wrf_namelist({"dx_m": 37.037037, "dy_m": 37.037037, "e_we": 64, "e_sn": 64}, 7)
        self.assertIn("time_step = 0, time_step_fract_num = 125000, time_step_fract_den = 1000000", namelist)
        self.assertIn("starting_time_step = 125000, starting_time_step_den = 1000000", namelist)
        self.assertIn("min_time_step = 31250, min_time_step_den = 1000000", namelist)
        self.assertIn("max_time_step = 125000, max_time_step_den = 1000000", namelist)

        d08 = chain._wrf_namelist({"dx_m": 12.345679, "dy_m": 12.345679, "e_we": 127, "e_sn": 127}, 8)
        self.assertIn("use_adaptive_time_step = .false.", d08)
        self.assertIn("step_to_output_time = .false.", d08)
        resumed_d08 = chain._wrf_namelist(
            {"dx_m": 12.345679, "dy_m": 12.345679, "e_we": 127, "e_sn": 127},
            8,
            datetime(2026, 9, 15, 3, tzinfo=timezone.utc),
            time_step_scale=0.5,
        )
        self.assertIn("time_step_fract_num = 31250", resumed_d08)

    def test_d08_resume_uses_latest_post_start_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chain = chain_module.Chain.__new__(chain_module.Chain)
            chain.start = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
            chain.end = datetime(2026, 9, 18, 18, tzinfo=timezone.utc)
            chain.binary_hashes = {"wrf.exe": "current-wrf-sha256"}
            chain.topology_sha256 = "current-topology-sha256"
            (root / "namelist.input").write_text(
                "use_adaptive_time_step = .false., step_to_output_time = .false.,", encoding="utf-8"
            )
            (root / "wrf-run-identity.json").write_text(json.dumps({
                "wrf_binary_sha256": "current-wrf-sha256",
                "topology_sha256": "current-topology-sha256",
                "fixed_time_step": True,
            }), encoding="utf-8")
            (root / "wrfrst_d01_2026-09-18_13:00:00").write_bytes(b"restart")
            (root / "wrfrst_d01_2026-09-18_12:00:00").write_bytes(b"initial")
            value, path = chain._latest_restart(root)
            self.assertEqual(value, datetime(2026, 9, 18, 13, tzinfo=timezone.utc))
            self.assertEqual(path.name, "wrfrst_d01_2026-09-18_13:00:00")
            (root / "resume_from.json").write_text('{"attempt": 2}', encoding="utf-8")
            self.assertIsNotNone(chain._latest_restart(root))
            (root / "resume_from.json").write_text('{"attempt": 3}', encoding="utf-8")
            self.assertIsNone(chain._latest_restart(root))

    def test_d08_resume_rejects_restart_from_replaced_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chain = chain_module.Chain.__new__(chain_module.Chain)
            chain.start = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
            chain.end = datetime(2026, 9, 18, 18, tzinfo=timezone.utc)
            chain.binary_hashes = {"wrf.exe": "new-binary"}
            chain.topology_sha256 = "topology"
            (root / "namelist.input").write_text(
                "use_adaptive_time_step = .false., step_to_output_time = .false.,", encoding="utf-8"
            )
            (root / "wrf-run-identity.json").write_text(json.dumps({
                "wrf_binary_sha256": "old-binary",
                "topology_sha256": "topology",
                "fixed_time_step": True,
            }), encoding="utf-8")
            (root / "wrfrst_d01_2026-09-18_13:00:00").write_bytes(b"restart")
            self.assertIsNone(chain._latest_restart(root))

    def test_wps_resume_accepts_complete_frozen_checkpoint_after_grib_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            chain = chain_module.Chain.__new__(chain_module.Chain)
            chain.done = lambda _name: False
            chain._driver_cycle = lambda: (Path(directory) / "deleted-driver-cycle", 0)
            chain._frozen_wps_checkpoint = lambda: "frozen-inventory-sha256"
            chain.wps_prepare()

    def test_wrf_les_namelist_requires_runtime_turbulence_history_fields(self):
        chain = chain_module.Chain.__new__(chain_module.Chain)
        chain.start = datetime(2026, 9, 15, tzinfo=timezone.utc)
        chain.end = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
        chain.wrf = {"e_vert": 88}
        chain._common_time = lambda domains, start=None: "start_year = 2026,"
        namelist = chain._wrf_namelist({"dx_m": 37.037037, "dy_m": 37.037037, "e_we": 64, "e_sn": 64}, 7)
        self.assertIn("restart = .false.", namelist)
        self.assertIn('iofields_filename = "wrf_les_iofields.txt"', namelist)
        self.assertIn("ignore_iofields_warning = .false.", namelist)

        with tempfile.TemporaryDirectory() as directory:
            chain._write = lambda path, content: Path(path).write_text(content, encoding="utf-8")
            chain._write_wrf_iofields(Path(directory))
            self.assertEqual((Path(directory) / "wrf_les_iofields.txt").read_text(encoding="utf-8").strip().splitlines()[-1], "+:h:0:tke")

    def _entries(self, root):
        start = datetime(2026, 9, 15, tzinfo=timezone.utc)
        entries = []
        for index in range(49):
            path = root / "forcing" / ("d08-%02d.nc" % index)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("payload-%02d" % index).encode())
            entries.append({
                "valid_time_utc": (start + timedelta(hours=index)).isoformat().replace("+00:00", "Z"),
                "path": str(path.relative_to(root)),
                "size_bytes": path.stat().st_size,
                "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "sha256": chain_module.sha256_file(path),
            })
        return entries, [item["valid_time_utc"] for item in entries]

    def test_inventory_rejects_missing_duplicate_size_and_hash_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries, times = self._entries(root)
            chain_module.validate_forcing_inventory(root, entries, times)

            cases = ("missing", "duplicate", "size", "mtime", "hash")
            for case in cases:
                changed = deepcopy(entries)
                if case == "missing":
                    (root / changed[0]["path"]).unlink()
                elif case == "duplicate":
                    changed[1]["valid_time_utc"] = changed[0]["valid_time_utc"]
                elif case == "size":
                    changed[0]["size_bytes"] += 1
                elif case == "mtime":
                    changed[0]["mtime_utc"] = "2000-01-01T00:00:00Z"
                else:
                    changed[0]["sha256"] = "0" * 64
                with self.subTest(case=case):
                    with self.assertRaises(RuntimeError):
                        chain_module.validate_forcing_inventory(root, changed, times)

    def test_source_inventory_rechecks_file_size_mtime_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "wrfout_d04_2026-09-15_00:00:00"
            path.write_bytes(b"source")
            stat = path.stat()
            item = {
                "name": path.name,
                "kind": "history",
                "size_bytes": stat.st_size,
                "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "sha256": chain_module.sha256_file(path),
                "stable": True,
            }
            chain = chain_module.Chain.__new__(chain_module.Chain)
            chain.source_run = root
            chain.source_artifact = {"files": [item]}
            chain._validate_source_inventory()
            path.write_bytes(b"changed")
            with self.assertRaises(RuntimeError):
                chain._validate_source_inventory()

    def test_marker_contract_identity_mismatch_cannot_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker_dir = root / "stage_markers"
            marker_dir.mkdir()
            marker = {
                "contract_sha256": "contract-a",
                "topology_sha256": "topology-a",
                "input_inventory_sha256": "input-a",
                "wrf_binary_sha256": {"wrf.exe": "binary-a"},
                "stage_config_sha256": "config-a",
                "run_dir": str(root),
            }
            (marker_dir / "wrf_d05_333m.json").write_text(json.dumps(marker), encoding="utf-8")
            chain = chain_module.Chain.__new__(chain_module.Chain)
            chain.root = root
            chain.contract_sha256 = "contract-a"
            chain.topology_sha256 = "topology-a"
            chain.binary_hashes = {"wrf.exe": "binary-a"}
            chain.stage_config_sha256 = "config-a"
            chain._input_hash = lambda: "input-a"
            chain._stage_outputs_valid = lambda name: True
            self.assertTrue(chain.done("wrf_d05_333m"))
            chain.contract_sha256 = "contract-b"
            self.assertFalse(chain.done("wrf_d05_333m"))
            chain.contract_sha256 = "contract-a"
            chain.stage_config_sha256 = "config-b"
            self.assertFalse(chain.done("wrf_d05_333m"))

    def test_runner_contract_carries_native_upstream_inventory(self):
        request = DownscaleValidationRequest(
            upstream_mode="full_chain",
            source_wrf_task_id="wrf_gfs_20260915T000000Z_abcdef12",
            start_time=datetime(2026, 9, 15, tzinfo=timezone.utc),
            execution_scope="forcing_only",
            aoi={"center_lat": 32, "center_lon": 118, "horizontal_crs": "EPSG:4326", "width_m": 500, "height_m": 500},
        )
        inventory = [{
            "name": "wrfout_d04_2026-09-15_00:00:00",
            "kind": "history",
            "size_bytes": 123,
            "mtime_utc": "2026-09-16T00:00:00Z",
            "sha256": "a" * 64,
            "stable": True,
        }]
        contract = build_runner_contract(
            "ds_20260915T000000Z_abcdef12",
            request.model_dump(mode="json"),
            {"source_wrf": {"artifact": {"remote_output_dir": "/data/WRF/run", "files": inventory}}},
        )
        self.assertEqual(contract["upstream"]["source_artifact"]["files"], inventory)
        self.assertEqual(contract["upstream"]["source_artifact"]["remote_output_dir"], "/data/WRF/run")


if __name__ == "__main__":
    unittest.main()
