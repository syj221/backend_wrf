from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings
from downscale.product import sha256_file, validate_product_bundle
from downscale.schemas import (
    CertificationRequest,
    CertificationReview,
    DownscaleTaskCreate,
    DownscaleValidationRequest,
    FORCING_DX_M,
    FORCING_SCHEMA_VERSION,
    FORCING_TOPOLOGY_ID,
    TerrainAssetReview,
    TerrainAssetSubmission,
    WrfForcingSpec,
    WrfNestDomain,
    WrfPhysicsSpec,
    valid_times,
)
from downscale.service import DownscaleService
from downscale.profiles import DEFAULT_PARAMETERIZATION_PROFILE_ID
from downscale.upstream import build_source_artifact


ASSET_KINDS = ["dtm", "dsm", "buildings", "stl", "land_cover", "surface_properties"]


class DownscaleServiceTests(unittest.TestCase):
    def test_source_artifact_uses_scheduler_selected_driver_cycle(self):
        source = {
            "id": "wrf_auto", "status": "succeeded",
            "request": {
                "data_source": "gfs", "start_time": "2026-09-18T12:00:00Z", "end_time": "2026-09-18T18:00:00Z",
                "center": {"lat": 40.0, "lon": 116.4}, "domains": [{"id": "d04", "dx": 1000}],
            },
            "runtime": {"forcing_cycle": "2026091800", "forecast_hours": list(range(6, 25))},
        }
        inventory = {"remote_output_dir": "/data/run", "files": [
            {"name": "wrfout_d04_2026-09-18_12:00:00", "kind": "history", "size_bytes": 1, "mtime_utc": "2026-09-18T19:00:00Z", "sha256": "a" * 64, "stable": True},
            {"name": "wrfrst_d04_2026-09-18_12:00:00", "kind": "restart", "size_bytes": 1, "mtime_utc": "2026-09-18T19:00:00Z", "sha256": "b" * 64, "stable": True},
        ]}
        artifact = build_source_artifact(source, inventory)
        self.assertEqual(artifact["source_cycle"], "2026091800")
        self.assertEqual(artifact["source_forecast_hours"], list(range(6, 25)))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.catalog = root / "terrain.json"
        self.catalog.write_text(json.dumps({"items": [{
            "id": "pilot-professional-v2", "label": "试点专业地形", "version": "v2",
            "status": "approved", "checksum_sha256": "a" * 64,
            "horizontal_crs": "EPSG:32650", "vertical_datum": "EGM2008",
            "coverage_width_m": 3_500, "coverage_height_m": 3_500,
            "coverage_center_lat": 32.06, "coverage_center_lon": 118.80,
            "hchar_m": 10, "control_point_error_m": 0.25,
            "source_file_uuids": ["upload-dtm", "upload-dsm", "upload-surface"],
            "asset_kinds": ASSET_KINDS,
            "asset_files": [{"file_uuid": f"upload-{kind}", "kind": kind, "file_name": f"{kind}.bin", "size_bytes": 1, "sha256": "a" * 64} for kind in ASSET_KINDS],
            "source": "backend_upload",
        }]}), encoding="utf-8")
        self.config = replace(
            settings,
            data_dir=root / "data", run_dir=root / "data" / "runs",
            catalog_publish_enabled=False, downscale_database_path=root / "tasks.sqlite3",
            downscale_terrain_catalog_path=self.catalog, downscale_confirmation_secret="test-secret",
            downscale_available_memory_gib=128, downscale_available_disk_gib=208,
        )
        self.normal_tasks = {
            "wrf_1km": {"id": "wrf_1km", "owner_sub": "user-a", "status": "succeeded", "request": {"domains": [{"dx": 1000}]}},
            "wrf_9km": {"id": "wrf_9km", "owner_sub": "user-a", "status": "succeeded", "request": {"domains": [{"dx": 9000}]}},
            "wrf_running": {"id": "wrf_running", "owner_sub": "user-a", "status": "running", "request": {"domains": [{"dx": 1000}]}},
        }
        self.service = DownscaleService(self.config, normal_task_lookup=self._lookup_normal_task)
        legacy_domains = [
            WrfNestDomain(
                name=f"d{index + 1:02d}", dx_m=25_000.0 / (3 ** index), dy_m=25_000.0 / (3 ** index),
                parent_grid_ratio=1 if index == 0 else 3, e_we=64, e_sn=64,
                parent_start_i=11 if index else 1, parent_start_j=11 if index else 1,
            )
            for index in range(7)
        ]
        legacy_wrf = WrfForcingSpec(
            domains=legacy_domains,
            physics=WrfPhysicsSpec(
                mp_physics=8, ra_lw_physics=4, ra_sw_physics=4, bl_pbl_physics=1,
                sf_sfclay_physics=1, sf_surface_physics=2, cu_physics_by_domain=[0] * 7, radt_minutes=5,
            ),
            output_variables=["U", "V", "W", "T", "P", "PB", "PH", "PHB", "QVAPOR", "QKE", "HGT", "UST", "HFX", "PBLH"],
        )
        self.request = DownscaleValidationRequest(
            upstream_mode="full_chain", start_time=datetime(2026, 8, 13, tzinfo=timezone.utc),
            source_wrf_task_id="wrf_1km",
            terrain_asset_id="pilot-professional-v2",
            execution_scope="wrf_cfd", wrf=legacy_wrf,
            aoi={
                "center_lat": 32.06, "center_lon": 118.80, "horizontal_crs": "EPSG:32650",
                "center_x_m": 481_000, "center_y_m": 3_548_000, "width_m": 500, "height_m": 500,
            },
        )

    def _lookup_normal_task(self, task_id: str, owner_sub: str, owner_role: int):
        task = self.normal_tasks.get(task_id)
        if task and (owner_role >= 3 or task["owner_sub"] == owner_sub):
            return task
        return None

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def _create_task(self, key: str = "same-key"):
        preflight = self.service.validate(self.request, "user-a")
        payload = DownscaleTaskCreate(**self.request.model_dump(), confirmation_token=preflight["confirmation_token"])
        return self.service.create(payload, "user-a", key)

    def test_source_wrf_must_be_completed_at_1km(self):
        nine_km = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"source_wrf_task_id"}), source_wrf_task_id="wrf_9km"
        )
        with self.assertRaisesRegex(ValueError, "最细域必须为 1 km"):
            self.service.validate(nine_km, "user-a")

        running = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"source_wrf_task_id"}), source_wrf_task_id="wrf_running"
        )
        with self.assertRaisesRegex(ValueError, "必须成功完成"):
            self.service.validate(running, "user-a")

    def test_dynamic_window_generates_hourly_contract_and_sampling(self):
        for duration, selected_hours in ((1, [0, 1]), (6, [0, 3, 6]), (24, [0, 12, 24]), (48, [0, 24, 48])):
            request = DownscaleValidationRequest(
                **self.request.model_dump(exclude={"end_time", "cfd"}),
                end_time=self.request.start_time + timedelta(hours=duration),
                cfd={
                    **self.request.cfd.model_dump(),
                    "rans": {"selected_hours": selected_hours, "selection_mode": "user_confirmed"},
                },
            )
            preflight = self.service.validate(request, "user-a")
            expected = valid_times(request.start_time, request.end_time)
            self.assertEqual(len(preflight["forcing"]["required_valid_times"]), duration + 1)
            self.assertEqual(preflight["sampling"]["selected_valid_times"], [expected[hour] for hour in selected_hours])
            self.assertEqual(preflight["forcing"]["required_valid_times"], expected)

    def test_new_task_creation_rejects_legacy_34m_mode(self):
        with self.assertRaisesRegex(ValueError, "reuse_34m.*不能创建"):
            DownscaleTaskCreate(
                **self.request.model_dump(exclude={"upstream_mode", "upstream_artifact_id", "upstream_content_sha256"}),
                upstream_mode="reuse_34m", upstream_artifact_id="legacy", upstream_content_sha256="a" * 64,
                confirmation_token="x" * 32,
            )

    def test_auto_upstream_request_keeps_normal_four_domain_topology(self):
        request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"wrf", "end_time", "cfd"}),
            end_time=self.request.start_time + timedelta(hours=6),
            cfd={**self.request.cfd.model_dump(), "rans": {"selected_hours": [0, 6]}},
            wrf=WrfForcingSpec(),
        )
        upstream = self.service._auto_upstream_request(request)
        x, y = upstream["center"]["grid_x"], upstream["center"]["grid_y"]
        for domain in request.wrf.domains[1:]:
            x = (x - domain.parent_start_i) * domain.parent_grid_ratio + 1
            y = (y - domain.parent_start_j) * domain.parent_grid_ratio + 1
            self.assertTrue(1 < x < domain.e_we)
            self.assertTrue(1 < y < domain.e_sn)
        self.assertAlmostEqual(x, (request.wrf.domains[-1].e_we + 1) / 2)
        self.assertAlmostEqual(y, (request.wrf.domains[-1].e_sn + 1) / 2)
        self.assertEqual(len(upstream["domains"]), 4)
        self.assertEqual(upstream["domains"][-1]["dx"], 1000)
        self.assertEqual(upstream["end_time"], request.end_time.isoformat().replace("+00:00", "Z"))

    def test_full_chain_create_records_auto_upstream_child(self):
        submitted = {}

        def submit(request, owner, key):
            submitted.update(request=request, owner=owner, key=key)
            return {"id": "wrf_auto_1km", "status": "queued"}

        service = DownscaleService(
            self.config,
            normal_task_lookup=self._lookup_normal_task,
            normal_task_submit=submit,
        )
        try:
            request = DownscaleValidationRequest(**self.request.model_dump(exclude={"source_wrf_task_id", "end_time", "cfd", "wrf"}), source_wrf_task_id=None, end_time=self.request.start_time + timedelta(hours=6), cfd={**self.request.cfd.model_dump(), "rans": {"selected_hours": [0, 6]}}, wrf=WrfForcingSpec())
            preflight = service.validate(request, "user-a")
            task = service.create(
                DownscaleTaskCreate(**request.model_dump(), confirmation_token=preflight["confirmation_token"]),
                "user-a", "auto-upstream",
            )
            self.assertEqual(task["runtime"]["source_wrf"]["task_id"], "wrf_auto_1km")
            self.assertEqual(task["runtime"]["source_wrf"]["auto_created"], True)
            self.assertEqual(submitted["key"], f"highres-upstream-{task['id']}")
            self.assertEqual(submitted["request"]["domains"][-1]["dx"], 1000)
            self.assertEqual(submitted["request"]["highres_parent_task_id"], task["id"])
        finally:
            service.close()

    def test_auto_upstream_network_retry_reuses_reservation_and_child_key(self):
        calls = []
        def submit(request, owner, key):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("response lost after upstream acceptance")
            return {"id": "wrf_auto_1km", "status": "queued"}
        self.service.normal_task_submit = submit
        request = DownscaleValidationRequest(
            start_time="2026-09-17T06:00:00Z", end_time="2026-09-17T12:00:00Z",
            aoi=self.request.aoi, cfd={"rans": {"selected_hours": [0, 6]}},
        )
        checked = self.service.validate(request, "user-a")
        payload = DownscaleTaskCreate(**request.model_dump(), confirmation_token=checked["confirmation_token"])
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            self.service.create(payload, "user-a", "retry-submit")
        task = self.service.create(payload, "user-a", "retry-submit")
        again = self.service.create(payload, "user-a", "retry-submit")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(len(calls), 2)
        self.assertEqual(task["id"], again["id"])
        with self.assertRaises(KeyError):
            self.service.get_visible(task["id"], "user-b", 1)

    def test_failed_auto_child_retry_creates_linked_attempt_once(self):
        submitted = []
        self.service.normal_task_submit = lambda request, owner, key: (submitted.append((request, key)) or {"id":"wrf_retry", "status":"queued"})
        self.request = self.request.model_copy(update={"end_time": self.request.start_time + timedelta(hours=6), "cfd": self.request.cfd.model_copy(update={"rans": self.request.cfd.rans.model_copy(update={"selected_hours":[0,6]})})})
        task = self._create_task("parent-retry")
        runtime = dict(task["runtime"])
        runtime["source_wrf"] = {"task_id":"wrf_failed", "auto_created":True}
        self.normal_tasks["wrf_failed"] = {"id":"wrf_failed", "status":"failed", "owner_sub":"user-a"}
        self.service.store.update(task["id"], runtime=runtime, status="failed")
        result = self.service.retry(task["id"], "user-a", 1)
        self.assertEqual(result["runtime"]["source_wrf"]["task_id"], "wrf_retry")
        self.assertEqual(submitted[0][0]["highres_parent_task_id"], task["id"])
        with self.assertRaises(ValueError):
            self.service.retry(task["id"], "user-a", 1)

    def test_source_artifact_requires_native_restart_and_history_files(self):
        source = {
            "id": "wrf_1km",
            "status": "succeeded",
            "request": {"domains": [{"id": "d04", "dx": 1000}]},
        }
        inventory = {
            "remote_output_dir": "/remote/WRF_wrf_1km/run",
            "inventory_sha256": "f" * 64,
            "files": [{"name": "wrfout_d04_2026-08-13_06:00:00", "kind": "history", "size_bytes": 10, "stable": True}],
        }
        with self.assertRaisesRegex(ValueError, "缺少 wrfrst"):
            build_source_artifact(source, inventory)
        inventory["files"].append({"name": "wrfrst_d04_2026-08-13_06:00:00", "kind": "restart", "size_bytes": 10, "stable": True})
        artifact = build_source_artifact(source, inventory)
        self.assertEqual(artifact["status"], "ready")
        self.assertEqual(artifact["restart_file_count"], 1)

    def test_source_artifact_scopes_inventory_to_d04(self):
        source = {
            "id": "wrf_1km",
            "status": "succeeded",
            "request": {"domains": [{"id": "d04", "dx": 1000}]},
        }
        inventory = {
            "files": [
                {"name": "wrfout_d03_2026-08-13_06:00:00", "kind": "history", "size_bytes": 10, "stable": True},
                {"name": "wrfout_d04_2026-08-13_06:00:00", "kind": "history", "size_bytes": 10, "stable": True},
                {"name": "wrfrst_d04_2026-08-13_06:00:00", "kind": "restart", "size_bytes": 10, "stable": True},
            ],
        }
        artifact = build_source_artifact(source, inventory)
        self.assertEqual([item["name"] for item in artifact["files"]], [
            "wrfout_d04_2026-08-13_06:00:00", "wrfrst_d04_2026-08-13_06:00:00",
        ])

    def _bundle(self, task_id: str, request: DownscaleValidationRequest, *, continuous: bool = False) -> Path:
        root = Path(self.temp.name) / f"bundle-{task_id}"
        root.mkdir()
        files = {
            "statistics.nc": b"netcdf", "wind_speed_10m.tif": b"geotiff",
            "scene.vtu": b"vtk", "quality.pdf": b"quality", "preview.webp": b"preview",
        }
        for name, content in files.items():
            (root / name).write_bytes(content)
        assets = [
            {"path": name, "size_bytes": (root / name).stat().st_size, "sha256": sha256_file(root / name)}
            for name in files
        ]
        manifest = {
            "schema_version": "downscale-product/v2", "workflow_id": task_id,
            "product_kind": "wrf_to_cfd_rans_snapshot_set",
            "surface_model": {"mode": request.surface_mode},
            "mesoscale_forcing": {"valid_times_utc": valid_times(request.start_time), "horizontal_spacing_m": 34.293556},
            "publication_product": {"width_m": request.aoi.width_m, "height_m": request.aoi.height_m, "horizontal_sampling_m": 1},
            "temporal_semantics": {"kind": "independent_representative_steady_snapshots", "continuous_1m_time_series": continuous, "temporal_interpolation": "forbidden"},
            "rans_snapshots": [{"forcing": {"temporal_match": "exact"}, "solver": {"solution_type": "steady_rans"}}],
            "standard_products": {"cf_netcdf": "statistics.nc", "geotiff_layers": ["wind_speed_10m.tif"], "vtk_scene": "scene.vtu", "quality_report": "quality.pdf"},
            "assets": assets, "quality_gates": [{"id": "mesh", "status": "pass"}],
        }
        (root / "downscale.product.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_rans_preflight_and_idempotent_create(self):
        preflight = self.service.validate(self.request, "user-a")
        self.assertEqual(preflight["product"]["width_m"], 500)
        self.assertEqual(preflight["cfd_domain"]["width_m"], 1100)
        self.assertEqual(preflight["sampling"]["solver_family"], "rans")
        self.assertEqual(preflight["certification"]["status"], "research")
        payload = DownscaleTaskCreate(**self.request.model_dump(), confirmation_token=preflight["confirmation_token"])
        task = self.service.create(payload, "user-a", "same-key")
        again = self.service.create(payload, "user-a", "same-key")
        self.assertEqual(task["id"], again["id"])
        self.assertFalse(task["runtime"]["microscale_sampling"]["continuous_1m_time_series"])
        self.assertEqual(task["runtime"]["runner_contract"]["schema_version"], "wrf-cfd-managed-job/v3")
        contract_path = self.config.run_dir / "downscale_jobs" / task["id"] / "downscale.job.json"
        self.assertTrue(contract_path.is_file())
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertEqual(contract["parameterization"]["id"], DEFAULT_PARAMETERIZATION_PROFILE_ID)
        self.assertEqual(contract["parameterization"]["final_parameters"]["cfd"], self.request.cfd.model_dump(mode="json"))
        self.assertEqual(contract["configuration_hash"], task["runtime"]["configuration_hash"])

    def test_auto_flat_surface_needs_no_terrain_and_is_audited(self):
        request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"terrain_asset_id", "surface_mode"}),
            surface_mode="auto_flat", terrain_asset_id=None,
        )
        preflight = self.service.validate(request, "user-a")
        self.assertEqual(preflight["surface"]["mode"], "auto_flat")
        self.assertEqual(preflight["surface"]["roughness"]["required_variables"], ["LU_INDEX", "ZNT"])
        self.assertEqual(preflight["cfd_domain"]["buffer_m"], 300)
        payload = DownscaleTaskCreate(**request.model_dump(), confirmation_token=preflight["confirmation_token"])
        task = self.service.create(payload, "user-a", "flat-surface")
        self.assertEqual(task["runtime"]["surface"]["geometry"]["kind"], "generated_flat_prism")
        self.assertIsNone(task["runtime"]["terrain"])

    def test_parameterization_profiles_are_versioned_and_exposed(self):
        templates = self.service.templates()
        profiles = templates["parameterization"]["profiles"]
        self.assertEqual(templates["parameterization"]["default_profile_id"], DEFAULT_PARAMETERIZATION_PROFILE_ID)
        self.assertEqual(
            [profile["id"] for profile in profiles],
            ["standard-rans-thermal/v1", "neutral-rans/v1", "research-les-episode/v1"],
        )
        for profile in profiles:
            self.assertEqual(profile["schema_version"], "wrf-cfd-parameterization/v1")
            self.assertTrue(profile["resource_estimate"]["end_to_end_runtime_hours"])
            self.assertTrue(profile["output_type"])
            self.assertIn("formal_release_allowed", profile)
            self.assertIn("editable_parameters", profile)
            self.assertIn("fixed_parameters", profile)

    def test_profile_default_and_expert_customization_are_recorded(self):
        production = self.request.model_copy(update={"wrf": WrfForcingSpec(), "parameterization_profile_id": DEFAULT_PARAMETERIZATION_PROFILE_ID})
        default = self.service.validate(production, "user-a")
        self.assertEqual(default["parameterization"]["id"], DEFAULT_PARAMETERIZATION_PROFILE_ID)
        self.assertFalse(default["parameterization"]["customized"])
        self.assertEqual(default["execution_plan"]["stages"][-1], "cfd_product_ready")

        custom = production.model_copy(update={
            "cfd": production.cfd.model_copy(update={"thermal_mode": "neutral"}),
        })
        custom_result = self.service.validate(custom, "user-a")
        self.assertTrue(custom_result["parameterization"]["customized"])
        self.assertEqual(custom_result["parameterization"]["configuration_status"], "customized_research")
        self.assertNotEqual(default["certification"]["recipe_hash"], custom_result["certification"]["recipe_hash"])

    def test_unknown_parameterization_profile_is_rejected(self):
        request = self.request.model_copy(update={"parameterization_profile_id": "missing-profile/v1"})
        with self.assertRaisesRegex(ValueError, "未知高分参数化方案"):
            self.service.validate(request, "user-a")

    def test_les_is_a_separate_finite_statistics_contract(self):
        request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"cfd"}),
            cfd={
                "solver_family": "les", "thermal_mode": "neutral", "solver_template_id": "neutral-les/v1",
                "rans": None,
                "les": {"episodes": [
                    {"forcing_hour": 6, "inflow_method": "synthetic_turbulence"},
                    {"forcing_hour": 18, "inflow_method": "precursor_abl"},
                ]},
            },
        )
        preflight = self.service.validate(request, "user-a")
        self.assertEqual(preflight["sampling"]["solver_family"], "les")
        self.assertEqual(len(preflight["sampling"]["episodes"]), 2)
        self.assertFalse(preflight["sampling"]["continuous_1m_time_series"])

    def test_large_aoi_becomes_review_task_without_mesh_coarsening(self):
        request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"aoi"}),
            aoi={**self.request.aoi.model_dump(), "width_m": 2_100, "height_m": 2_100},
        )
        preflight = self.service.validate(request, "user-a")
        self.assertEqual(preflight["cfd_domain"]["queue_mode"], "review")
        self.assertIn("AOI 最长边超过 2 km", preflight["cfd_domain"]["review_reasons"])
        task = self.service.create(
            DownscaleTaskCreate(**request.model_dump(), confirmation_token=preflight["confirmation_token"]), "user-a", "large-aoi",
        )
        self.assertEqual(task["status"], "pending_review")
        self.assertEqual(task["stage"], "resource_scientific_review")

    def test_reused_forcing_requires_certification_variables_and_datum(self):
        manifest = {
            "quality_status": "mesoscale_forcing_complete", "certification": {"status": "certified"},
            "valid_times_utc": valid_times(self.request.start_time), "coverage_width_m": 1_400,
            "coverage_height_m": 1_400, "vertical_top_m_agl": 500, "vertical_layer_count": 88,
            "layers_above_cfd_top": 2, "variables": sorted({"U", "V", "W", "T", "P", "PB", "PH", "PHB", "QVAPOR", "UST", "HFX", "PBLH"}),
            "horizontal_crs": "EPSG:32650", "vertical_datum": "EGM2008",
        }
        digest = hashlib.sha256(b"forcing").hexdigest()
        self.service.store.register_forcing("ms34-demo", digest, manifest, "user-a")
        reuse = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"upstream_mode", "upstream_artifact_id", "upstream_content_sha256"}),
            upstream_mode="reuse_34m", upstream_artifact_id="ms34-demo", upstream_content_sha256=digest,
        )
        self.assertEqual(self.service.validate(reuse, "user-a")["forcing"]["status"], "eligible")

    def test_terrain_submission_requires_review_before_use(self):
        submitted = self.service.submit_terrain_asset(TerrainAssetSubmission(
            id="new-terrain-v1", label="新地形", version="v1", checksum_sha256="b" * 64,
            horizontal_crs="EPSG:32650", vertical_datum="EGM2008", coverage_width_m=3_000,
            coverage_height_m=3_000, hchar_m=12, control_point_error_m=0.2,
            source_file_uuids=["backend-upload-1"], asset_kinds=ASSET_KINDS,
        ), "user-a")
        reviewed = self.service.review_terrain_asset(submitted["submission_id"], TerrainAssetReview(approved=True, note="control points verified"), "admin")
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual(self.service._terrain("new-terrain-v1")["status"], "approved")

    def test_reviewed_terrain_freezes_and_downloads_archived_files(self):
        configured = DownscaleService(replace(
            self.config, terrain_artifact_service_token="terrain-test-service-token-000000000",
        ), normal_task_lookup=self._lookup_normal_task)
        digest = hashlib.sha256(b"x").hexdigest()
        files = [{"file_uuid": f"file-{kind}", "kind": kind} for kind in ASSET_KINDS]

        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()

        def urlopen(request, timeout):
            uuid = request.full_url.rsplit("/", 2)[-2]
            kind = uuid.removeprefix("file-")
            if request.full_url.endswith("/manifest"):
                suffix = ".stl" if kind == "stl" else ".json" if kind == "surface_properties" else ".bin"
                return Response(json.dumps({"data": {"file_uuid": uuid, "file_name": f"{kind}{suffix}", "size_bytes": 1, "sha256": digest}}).encode())
            return Response(b"x")

        try:
            submission = configured.submit_terrain_asset(TerrainAssetSubmission(
                id="mapped-terrain-v1", label="映射地形", version="v1", checksum_sha256="c" * 64,
                horizontal_crs="EPSG:32650", vertical_datum="EGM2008", coverage_width_m=3000,
                coverage_height_m=3000, hchar_m=12, control_point_error_m=0.2,
                source_file_uuids=[item["file_uuid"] for item in files], asset_kinds=ASSET_KINDS,
                asset_files=files,
            ), "user-a")
            with patch("downscale.service.urllib.request.urlopen", side_effect=urlopen):
                reviewed = configured.review_terrain_asset(submission["submission_id"], TerrainAssetReview(approved=True), "admin")
                with tempfile.TemporaryDirectory() as temporary:
                    staged = configured.download_terrain_assets({"mode": "reviewed_terrain", "asset_files": reviewed["asset"]["asset_files"]}, Path(temporary))
                    self.assertEqual({name for _, name in staged}, {"dtm.bin", "dsm.bin", "buildings.bin", "stl.stl", "land_cover.bin", "surface_properties.json"})
                    self.assertTrue(all(path.read_bytes() == b"x" for path, _ in staged))
        finally:
            configured.close()

    def test_map_driven_terrain_resolution_requires_location_and_full_coverage(self):
        resolved = self.service.resolve_terrain_assets(self.request.aoi)
        self.assertEqual(resolved["next_action"], "select_approved_asset")
        self.assertEqual(resolved["ready_asset_ids"], ["pilot-professional-v2"])
        self.assertEqual(resolved["items"][0]["coverage_status"], "covered")
        outside = self.request.aoi.model_copy(update={"center_lon": 115.0})
        self.assertEqual(self.service.resolve_terrain_assets(outside)["next_action"], "request_or_prepare_terrain")

    def test_map_defined_aoi_does_not_require_manual_projected_coordinates(self):
        mapped = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"aoi"}),
            aoi={**self.request.aoi.model_dump(), "center_x_m": None, "center_y_m": None},
        )
        self.assertEqual(self.service.validate(mapped, "user-a")["product"]["sampling_m"], 1)

    def test_forcing_only_can_start_without_terrain_and_never_claims_a_1m_product(self):
        forcing_only = DownscaleValidationRequest(
            upstream_mode="full_chain", start_time=self.request.start_time,
            source_wrf_task_id=self.request.source_wrf_task_id, aoi=self.request.aoi.model_dump(),
            execution_scope="forcing_only",
            terrain_asset_id=None,
        )
        preflight = self.service.validate(forcing_only, "user-a")
        self.assertEqual(preflight["product"]["kind"], "mesoscale_forcing_only")
        self.assertIsNone(preflight["cfd_domain"])
        self.assertIsNone(preflight["sampling"])
        task = self.service.create(
            DownscaleTaskCreate(**forcing_only.model_dump(), confirmation_token=preflight["confirmation_token"]), "user-a", "forcing-only",
        )
        self.assertEqual(task["runtime"]["execution_scope"], "forcing_only")
        self.assertIsNone(task["runtime"]["terrain"])

    def _phase2_manifest(self, start_time):
        times = valid_times(start_time)
        return {
            "schema_version": FORCING_SCHEMA_VERSION,
            "topology_id": FORCING_TOPOLOGY_ID,
            "nominal_resolution_m": 10,
            "horizontal_spacing_m": FORCING_DX_M,
            "domain_count": 8,
            "final_domain": "d08",
            "quality_status": "mesoscale_forcing_complete",
            "quality": {"status": "pass"},
            "valid_times_utc": times,
            "forcing_files": [
                {"valid_time_utc": value, "path": "forcing/d08-%02d.nc" % index,
                 "size_bytes": 1, "sha256": "a" * 64}
                for index, value in enumerate(times)
            ],
            "vertical": {"layer_count": 88, "top_height_m": 2_000.0},
            "vertical_layer_count": 88,
            "variables": ["U", "V", "W", "T", "P", "PB", "PH", "PHB", "QVAPOR", "TKE", "HGT", "UST", "HFX", "PBLH"],
            "coverage": {"final_extent_m": {"x": 2_000.0, "y": 2_000.0}},
            "horizontal_crs": "EPSG:32650",
        }

    def _create_phase2_source(self, manifest=None, forcing_id="ds_20260813T000000Z_deadbeef"):
        forcing_request = DownscaleValidationRequest(
            upstream_mode="full_chain", start_time=self.request.start_time,
            source_wrf_task_id="wrf_1km", execution_scope="forcing_only", aoi=self.request.aoi.model_dump(),
        )
        remote_output_dir = "%s/WRFwork/DOWNSCALE_%s/run" % (self.config.hpc_storage_alias, forcing_id)
        runtime = {
            "execution_scope": "forcing_only",
            "source_wrf": {"task_id": "wrf_1km"},
            "remote_output_dir": remote_output_dir,
            "forcing_manifest": manifest or self._phase2_manifest(self.request.start_time),
        }
        self.service.store.create(
            forcing_id, "user-a", forcing_request.model_dump(mode="json"), runtime,
            status="succeeded", stage="done",
        )
        return forcing_id

    def test_completed_10m_forcing_is_the_only_phase2_cfd_input(self):
        forcing_id = self._create_phase2_source()
        request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"upstream_mode", "wrf", "source_forcing_task_id"}),
            upstream_mode="reuse_10m", source_forcing_task_id=forcing_id,
            wrf=WrfForcingSpec(),
        )
        preflight = self.service.validate(request, "user-a")
        self.assertEqual(preflight["forcing"]["status"], "eligible")
        self.assertEqual(preflight["forcing"]["schema_version"], FORCING_SCHEMA_VERSION)
        self.assertEqual(preflight["execution_plan"]["stages"][0], "source_forcing_handoff")
        self.assertEqual(preflight["execution_plan"]["cfd"]["input_dx_m"], FORCING_DX_M)

        payload = DownscaleTaskCreate(**request.model_dump(), confirmation_token=preflight["confirmation_token"])
        task = self.service.create(payload, "user-a", "phase2-cfd")
        contract = json.loads((self.config.run_dir / "downscale_jobs" / task["id"] / "downscale.job.json").read_text())
        self.assertEqual(contract["upstream"]["source_forcing"]["task_id"], forcing_id)
        self.assertEqual(contract["output_contract"]["mesoscale_forcing_schema_version"], FORCING_SCHEMA_VERSION)
        self.assertEqual(contract["output_contract"]["mesoscale_forcing_horizontal_spacing_m"], FORCING_DX_M)

    def test_phase2_rejects_old_or_incomplete_forcing_manifest(self):
        old_manifest = self._phase2_manifest(self.request.start_time)
        old_manifest["schema_version"] = "mesoscale-forcing/v1"
        old_id = self._create_phase2_source(old_manifest)
        request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"upstream_mode", "wrf", "source_forcing_task_id"}),
            upstream_mode="reuse_10m", source_forcing_task_id=old_id, wrf=WrfForcingSpec(),
        )
        with self.assertRaisesRegex(ValueError, "mesoscale-forcing/v2"):
            self.service.validate(request, "user-a")

        incomplete = self._phase2_manifest(self.request.start_time)
        incomplete["variables"].remove("TKE")
        incomplete_id = self._create_phase2_source(incomplete, "ds_20260813T000000Z_feedcafe")
        request = request.model_copy(update={"source_forcing_task_id": incomplete_id})
        with self.assertRaisesRegex(ValueError, "TKE/QKE"):
            self.service.validate(request, "user-a")

    def test_legacy_34m_topology_cannot_be_used_for_new_forcing_only_tasks(self):
        request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"execution_scope", "terrain_asset_id"}),
            execution_scope="forcing_only", terrain_asset_id=None,
        )
        with self.assertRaisesRegex(ValueError, "固定的 27 km"):
            self.service.validate(request, "user-a")

    def test_product_bundle_rejects_fake_continuous_forecast(self):
        root = self._bundle("ds_fake", self.request, continuous=False)
        self.assertEqual(validate_product_bundle(root, "ds_fake", self.request.model_dump(mode="json"))["workflow_id"], "ds_fake")
        manifest_path = root / "downscale.product.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["temporal_semantics"]["continuous_1m_time_series"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "连续 1 m 时间序列"):
            validate_product_bundle(root, "ds_fake", self.request.model_dump(mode="json"))

    def test_browser_preview_must_be_a_checked_lightweight_raster(self):
        root = self._bundle("ds_preview", self.request)
        manifest_path = root / "downscale.product.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["browser_preview"] = {"layers": {"10": {"wind_speed": "statistics.nc"}}}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "轻量 WebP 或 PNG"):
            validate_product_bundle(root, "ds_preview", self.request.model_dump(mode="json"))

    def test_les_bundle_is_checked_as_finite_statistics(self):
        root = self._bundle("ds_les", self.request)
        manifest_path = root / "downscale.product.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["product_kind"] = "wrf_to_cfd_les_episode_set"
        manifest["temporal_semantics"]["kind"] = "finite_les_time_window_statistics"
        manifest.pop("rans_snapshots")
        manifest["les_episodes"] = [{
            "forcing": {"temporal_match": "exact"},
            "solver": {"solution_type": "les_time_window_statistics"},
            "statistics_window": {"start_utc": "2026-08-13T00:10:00Z", "end_utc": "2026-08-13T00:40:00Z"},
        }]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(validate_product_bundle(root, "ds_les", self.request.model_dump(mode="json"))["product_kind"], "wrf_to_cfd_les_episode_set")

    def test_custom_config_publishes_internal_preview_then_can_be_certified(self):
        task = self._create_task("publish-key")
        completed = self.service.publish_bundle(task["id"], self._bundle(task["id"], self.request))
        self.assertEqual(completed["quality"]["status"], "complete")
        self.assertEqual(completed["publication"]["status"], "internal_preview")
        certification = self.service.request_certification(task["id"], "user-a", 1, CertificationRequest(evidence=["comparison/report-01"], note="regional comparison complete"))
        reviewed = self.service.review_certification(certification["id"], "admin", CertificationReview(approved=True, profile_id="regional-certified-v1"))
        self.assertEqual(reviewed["status"], "approved")
        certified_request = DownscaleValidationRequest(
            **self.request.model_dump(exclude={"certification_profile_id"}),
            certification_profile_id="regional-certified-v1",
        )
        self.assertEqual(self.service.validate(certified_request, "user-a")["certification"]["status"], "certified")


if __name__ == "__main__":
    unittest.main()
