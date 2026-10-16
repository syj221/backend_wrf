"""Compute-free checks of the installed-source catalogue and canonical files."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import f90nml
from catalog_source import build_catalog
from namelist_config import catalog, resolve, source_catalog, output_settings
from domain_preview import DomainPreviewRequest, preview_domains
from schemas import WrfTaskCreate
from hpc import build_task_config


def request():
    return {"data_source": "gfs", "start_time": "2026-09-30T06:00:00Z", "end_time": "2026-09-30T18:00:00Z",
        "center": {"lat": 32.06, "lon": 118.79}, "forecast_interval_hours": 1,
        "domains": [{"id": "d01", "dx": 9000, "dy": 9000, "e_we": 100, "e_sn": 100, "parent_id": 0}],
        "physics": {"mp_physics": 8, "bl_pbl_physics": 1, "sf_sfclay_physics": 1, "sf_surface_physics": 2, "num_land_cat": 21, "num_soil_layers": 4}}


def config(wps=None, input=None):
    return {"schema_version": catalog()["schema_version"], "source_hash": catalog()["source_hash"], "wps": wps or {}, "input": input or {}}


class NamelistTest(unittest.TestCase):
    def test_catalog_completeness_and_true_versions(self):
        c = catalog()
        self.assertEqual(c["wrf_version"], "4.7.1")
        self.assertEqual(c["wps_version"], "4.6.0")
        self.assertEqual(c["completeness"]["missing_compiled_fields"], [])
        self.assertEqual(sum(f["compiled"] and f["file"] == "input" for f in c["fields"]), c["completeness"]["compiled_wrf_fields"])
        self.assertEqual(set(c["binary_hashes"]), {"wrf", "real", "geogrid", "metgrid", "ungrib"})
        self.assertEqual(len({f["id"] for f in c["fields"]}), len(c["fields"]))

    def test_registry_short_rows_and_case_are_not_lost(self):
        c = build_catalog({"registry": {"Registry.EM_COMMON": 'rconfig integer ISFFLX namelist,physics 1 1'},
            "compiled_statements": 'NAMELIST /physics/ isfflx', "wps_sources": {}, "wrf_version": 'V4.7.1', "wps_version": 'Version 4.6.0', "source_hash": 'fixture', "binary_hashes": {}})
        self.assertEqual(c["fields"][0]["id"], "input.physics.isfflx")
        with self.assertRaises(ValueError):
            build_catalog({"registry": {}, "compiled_statements": 'NAMELIST /physics/ missing', "wps_sources": {}, "wrf_version": '', "wps_version": '', "source_hash": '', "binary_hashes": {}})

    def test_roundtrip_and_cross_file_dates_geometry(self):
        r = resolve(request())
        wps, wrf = (f90nml.reads(r["previews"][file]) for file in ("wps", "input"))
        self.assertEqual(wps["share"]["start_date"], "2026-09-30_00:00:00")
        self.assertEqual(wrf["time_control"]["run_hours"], 18)
        self.assertEqual(wrf["domains"]["e_we"], wps["geogrid"]["e_we"])
        self.assertEqual(wrf["physics"]["num_land_cat"], 21)
        self.assertEqual(wrf["physics"]["num_soil_layers"], 4)
        self.assertGreater(len(r["required_confirmation"]), 200)
        self.assertFalse(r["execution_verified"])

    def test_scalar_domain_and_array_validation(self):
        r = request(); r["domains"] += [{"id": "d02", "dx": 3000, "dy": 3000, "e_we": 61, "e_sn": 61, "parent_id": 1, "parent_grid_ratio": 3, "i_parent_start": 11, "j_parent_start": 11}]
        r["namelist"] = config(input={"physics": {"mp_physics": 6}, "domains": {"eta_levels": [1-i/87 for i in range(88)]}})
        self.assertEqual(resolve(r)["namelist"]["input"]["physics"]["mp_physics"], [6, 6])
        for field, value in (("mp_physics", [6]), ("num_soil_layers", [4, 4]), ("mp_physics", True)):
            r["namelist"] = config(input={"physics": {field: value}})
            with self.subTest(field=field), self.assertRaises(ValueError):
                resolve(r)

    def test_conditional_modules_enter_and_leave_scope(self):
        r = request()
        names = lambda r: {f["id"] for f in resolve(r)["required_confirmation"]}
        self.assertFalse(any(n.startswith(("input.fdda.", "input.noah_mp.")) for n in names(r)))
        r["assimilation_scheme"] = "fdda_standard"
        self.assertIn("input.fdda.guv", names(r))
        r["namelist"] = config(input={"physics": {"sf_surface_physics": 4}})
        self.assertTrue(any(n.startswith("input.noah_mp.") for n in names(r)))
        r["namelist"] = config(input={"physics": {"sf_surface_physics": 2}, "noah_mp": {"opt_alb": 1}})
        self.assertFalse(any(n.startswith("input.noah_mp.") for n in names(r)))
        self.assertNotIn("opt_alb", resolve(r)["effective"].get("input", {}).get("noah_mp", {}))

    def test_managed_resources_modules_and_stale_catalog_rejected(self):
        for c in (config(wps={"geogrid": {"geog_data_path": '/user/path'}}), config(input={"chem": {"chem_opt": 1}}), {**config(), "source_hash": "stale"}):
            with self.assertRaises(ValueError):
                resolve({**request(), "namelist": c})

    def test_legacy_request_stays_legacy_and_new_files_override_shell_defaults(self):
        normalized = WrfTaskCreate.model_validate(request()).model_dump(mode="json")
        self.assertIsNone(normalized["namelist"])
        self.assertNotIn("namelist_plan", build_task_config("fixture", normalized, "2026093000", [0, 18]))
        normalized["namelist"] = config(input={"domains": {"e_vert": 60}})
        prepared = build_task_config("fixture", normalized, "2026093000", [0, 18])
        self.assertEqual(f90nml.reads(prepared["namelist_plan"]["previews"]["input"])["domains"]["e_vert"], 60)

    def test_runtime_mismatch_and_tampering_stop_before_writing(self):
        sys.path.insert(0, str(Path(__file__).parent / "scripts"))
        import apply_namelist_plan as writer
        self.addCleanup(lambda: sys.path.pop(0))
        r = WrfTaskCreate.model_validate(request()).model_dump(mode="json"); r["namelist"] = config()
        prepared = build_task_config("fixture", r, "2026093000", [0, 18])
        with TemporaryDirectory(prefix="wrf-namelist-") as directory:
            target = Path(directory) / "namelist.input"
            with patch.dict("os.environ", {"WRF_SOURCE_DIR": directory, "WPS_SOURCE_DIR": directory}), patch.object(writer, "snapshot", return_value={"source_hash": "changed", "binary_hashes": {}}), self.assertRaises(ValueError):
                writer.apply(prepared, "input", target)
            self.assertFalse(target.exists())
            runtime = {"source_hash": source_catalog()["source_hash"], "binary_hashes": source_catalog()["binary_hashes"]}
            with patch.dict("os.environ", {"WRF_SOURCE_DIR": directory, "WPS_SOURCE_DIR": directory, "WRF_NUM_METGRID_LEVELS": "34", "WRF_NUM_METGRID_SOIL_LEVELS": "4"}), patch.object(writer, "snapshot", return_value=runtime):
                writer.apply(prepared, "input", target)
                self.assertEqual(f90nml.read(target)["domains"]["num_metgrid_levels"], 34)
                prepared["namelist_plan"]["previews"]["input"] += "tampered"
                with self.assertRaises(ValueError):
                    writer.apply(prepared, "input", target)

    def test_projection_variants_and_output_organization(self):
        for name in ("lambert", "mercator", "polar"):
            result = preview_domains(DomainPreviewRequest.model_validate({"aoi": {"west":118.5,"east":119,"south":31.8,"north":32.3}, "resolution_m":3000, "projection":{"map_proj":name,"truelat1":32,"truelat2":32,"stand_lon":118.75}}))
            self.assertTrue(result["finest_aoi_covered"])
        r = {**request(), "namelist": config(input={"time_control": {"history_interval": 30, "frames_per_outfile": 4}})}
        self.assertEqual(output_settings(r, 0)[0], 1800)
        self.assertEqual(f90nml.reads(resolve(r)["previews"]["input"])["time_control"]["frames_per_outfile"], 4)


if __name__ == "__main__":
    unittest.main()
