"""Projection control points and partial-request advice stay compute-free."""

import unittest
import math
from pyproj import Transformer
from types import SimpleNamespace
from unittest.mock import patch

from domain_preview import DomainPreviewRequest, _outline, preview_domains
from planning import PlanningAssessRequest, assess


class PlanningTest(unittest.TestCase):
    def test_generated_nests_cover_aoi_at_requested_finest_resolution(self):
        for count in (1, 2, 3, 4):
            for ratio in (3, 5):
                if 1000 * ratio ** (count - 1) > 50000:
                    continue
                result = preview_domains(DomainPreviewRequest.model_validate({
                    "aoi": {"west": 115.5, "east": 116, "south": 33.6, "north": 34},
                    "resolution_m": 1000, "domain_count": count, "nesting_ratio": ratio,
                }))
                self.assertEqual(len(result["domains"]), count)
                self.assertEqual(result["domains"][-1]["dx"], 1000)
                self.assertTrue(result["finest_aoi_covered"])
                for domain in result["domains"][1:]:
                    self.assertEqual((domain["e_we"] - 1) % ratio, 0)
                    self.assertEqual(domain["parent_grid_ratio"], ratio)

    def test_impossible_nesting_is_rejected_not_silently_downgraded(self):
        with self.assertRaises(ValueError):
            preview_domains(DomainPreviewRequest.model_validate({
                "aoi": {"west": 115.5, "east": 116, "south": 33.6, "north": 34},
                "resolution_m": 9000, "domain_count": 4, "nesting_ratio": 3,
            }))
    @staticmethod
    def four_domains():
        return {
            "center": {"lat": 30, "lon": 106},
            "domains": [
                {"id": "d01", "dx": 27000, "dy": 27000, "e_we": 100, "e_sn": 100, "parent_id": 0},
                *[{"id": f"d0{i + 1}", "dx": dx, "dy": dx, "e_we": 61, "e_sn": 61,
                   "parent_id": i, "parent_grid_ratio": 3, "i_parent_start": 11, "j_parent_start": 11}
                  for i, dx in enumerate([9000, 3000, 1000], 1)],
            ],
        }

    def test_all_four_domains_against_independent_spherical_lambert(self):
        result = preview_domains(DomainPreviewRequest.model_validate(self.four_domains()))
        # Independent analytic tangent Lambert equations, not the production transformer.
        phi0 = math.radians(30)
        n = math.sin(phi0)
        f = math.cos(phi0) * math.tan(math.pi / 4 + phi0 / 2) ** n / n
        rho0 = 6370000 * f / math.tan(math.pi / 4 + phi0 / 2) ** n
        origin = -49.5 * 27000
        parent_dx = 27000
        for index, boundary in enumerate(result["boundaries"]):
            if index:
                origin += 10 * parent_dx
                parent_dx /= 3
            dx = boundary["dx"]
            expected = [origin - dx / 2, origin - dx / 2,
                        origin + (boundary["grid"][0] - .5) * dx,
                        origin + (boundary["grid"][1] - .5) * dx]
            self.assertEqual(boundary["projected_bounds_m"], expected)
            ring = boundary["polygon"]["coordinates"][0]
            for point, xy in zip([ring[i] for i in (0, 20, 40, 60)],
                                 [(expected[0], expected[1]), (expected[2], expected[1]),
                                  (expected[2], expected[3]), (expected[0], expected[3])]):
                lon, lat = map(math.radians, point)
                rho = 6370000 * f / math.tan(math.pi / 4 + lat / 2) ** n
                theta = n * (lon - math.radians(106))
                self.assertAlmostEqual(rho * math.sin(theta), xy[0], delta=.03)
                self.assertAlmostEqual(rho0 - rho * math.cos(theta), xy[1], delta=.03)

    def test_each_domain_move_snaps_or_changes_center_without_mutating_input(self):
        for index in range(4):
            payload = self.four_domains()
            payload["edit"] = {"operation": "move", "domain_id": f"d0{index + 1}",
                               "start": {"lat": 30, "lon": 106}, "end": {"lat": 30, "lon": 106.3}}
            result = preview_domains(DomainPreviewRequest.model_validate(payload))
            self.assertTrue(result["adjustments"])
            if index == 0:
                self.assertAlmostEqual(result["center"]["lon"], 106.3, places=6)
            else:
                moved = result["domains"][index]
                self.assertIsInstance(moved["i_parent_start"], int)
                self.assertGreater(moved["i_parent_start"], 11)
            self.assertEqual(payload["center"]["lon"], 106)
            self.assertEqual(payload["domains"][index].get("i_parent_start", 11), 11)

    def test_move_outside_parent_is_rejected_not_silently_clamped(self):
        payload = self.four_domains()
        payload["edit"] = {"operation": "move", "domain_id": "d04",
                           "start": {"lat": 30, "lon": 106}, "end": {"lat": 40, "lon": 116}}
        with self.assertRaises(ValueError):
            preview_domains(DomainPreviewRequest.model_validate(payload))

    def test_child_draw_for_each_nested_domain_snaps_to_parent_grid(self):
        inverse = Transformer.from_crs(
            "+proj=lcc +lat_1=30 +lat_2=30 +lat_0=30 +lon_0=106 +R=6370000 +units=m",
            "EPSG:4326", always_xy=True,
        )
        for index in range(1, 4):
            payload = self.four_domains()
            initial = preview_domains(DomainPreviewRequest.model_validate(payload))
            parent = initial["boundaries"][index - 1]
            west, south, east, north = parent["projected_bounds_m"]
            lon, lat = inverse.transform((west + east) / 2, (south + north) / 2)
            bounds = {"west": lon - .02, "east": lon + .02, "south": lat - .02, "north": lat + .02}
            payload["domains"] = payload["domains"][:index + 1]
            payload["edit"] = {"operation": "draw", "domain_id": f"d0{index + 1}", "bounds": bounds}
            result = preview_domains(DomainPreviewRequest.model_validate(payload))
            child = result["domains"][index]
            self.assertEqual(result["center"], initial["center"])
            self.assertEqual(result["domains"][:index], initial["domains"][:index])
            self.assertEqual(result["boundaries"][:index], initial["boundaries"][:index])
            self.assertEqual((child["e_we"] - 1) % 3, 0)
            self.assertEqual((child["e_sn"] - 1) % 3, 0)
            self.assertTrue(result["adjustments"])
            child_box = result["boundaries"][index]["projected_bounds_m"]
            self.assertGreaterEqual(child_box[0], west)
            self.assertLessEqual(child_box[2], east)

    def test_outer_draw_reports_snapping_and_requested_bounds(self):
        payload = self.four_domains()
        payload["domains"] = payload["domains"][:1]
        bounds = {"west": 105.5, "east": 106.5, "south": 29.5, "north": 30.5}
        payload.update(aoi=bounds, edit={"operation": "draw", "domain_id": "d01", "bounds": bounds})
        result = preview_domains(DomainPreviewRequest.model_validate(payload))
        self.assertTrue(result["aoi_covered"])
        self.assertEqual(result["requested_bounds"], bounds)
        self.assertTrue(result["adjustments"])

    def test_projected_boundary_traces_each_edge_in_order(self):
        identity = SimpleNamespace(transform=lambda x, y: (x, y))
        ring = _outline(0, 0, 100, 50, identity)
        self.assertEqual(ring[0], [0, 0])
        self.assertEqual(ring[19], [95, 0])
        self.assertEqual(ring[20], [100, 0])
        self.assertEqual(ring[40], [100, 50])
        self.assertEqual(ring[-1], ring[0])

    def test_missing_input_never_invents_center_or_time(self):
        value = assess(PlanningAssessRequest(goal="重庆降水模拟"), {"status": "checking"})
        self.assertIn("center_confirmation", value["missing_fields"])
        self.assertIn("time_window", value["missing_fields"])
        self.assertEqual(value["candidates"], [])
        self.assertIsNone(value["resource_estimate"]["runtime"])
        self.assertLessEqual(len(value["questions"]), 3)

    def test_literature_candidate_keeps_source_and_unverified_status(self):
        class FakeKnowledge:
            def recommend(self, **_kwargs):
                return {"status": "ok", "kb_version": "snapshot-v1", "source_sha256": "a" * 64,
                        "candidates": [{"paper_id": "paper-1", "title": "Example",
                                        "source_url": "https://example.org/paper",
                                        "paper_wrf_version": "4.x",
                                        "matched": {"region": "Chongqing", "dx_km": 9},
                                        "provenance": {"file": "/private/knowledge/04_DOMAIN_PHYSICS.csv", "row": 2,
                                                       "verification_status": "unverified_import"}}]}

        request = PlanningAssessRequest(goal="降水", resolution_m=9000, evidence_query={
            "target": "precipitation", "region": "Chongqing",
        })
        with patch("planning.KnowledgeStore", return_value=FakeKnowledge()):
            result = assess(request, {"status": "unknown"})
        candidate = result["literature_evidence"]["candidates"][0]
        self.assertEqual(candidate["verification_status"], "unverified_import")
        self.assertFalse(candidate["runnable"])
        self.assertEqual(candidate["source_file"], "04_DOMAIN_PHYSICS.csv")
        self.assertNotIn("/private/knowledge", str(result))
        self.assertNotIn("physics_proposal", candidate)

    def test_wps_reference_point_and_nested_domain(self):
        result = preview_domains(DomainPreviewRequest.model_validate({
            "center": {"lat": 29.563, "lon": 106.5516, "grid_x": 50.5, "grid_y": 40},
            "domains": [
                {"id": "d01", "dx": 27000, "dy": 27000, "e_we": 100, "e_sn": 79, "parent_id": 0},
                {"id": "d02", "dx": 9000, "dy": 9000, "e_we": 79, "e_sn": 61,
                 "parent_id": 1, "i_parent_start": 11, "j_parent_start": 11},
            ],
        }))
        self.assertEqual(len(result["boundaries"]), 2)
        outer, inner = result["boundaries"]
        self.assertEqual(outer["projected_bounds_m"][0], -(49.5 * 27000 + 13500))
        self.assertGreater(inner["projected_bounds_m"][0], outer["projected_bounds_m"][0])
        self.assertLess(inner["projected_bounds_m"][2], outer["projected_bounds_m"][2])
        self.assertEqual(outer["polygon"]["coordinates"][0][0], outer["polygon"]["coordinates"][0][-1])

    def test_aoi_snap_reports_coverage_and_unconfirmed_center(self):
        result = preview_domains(DomainPreviewRequest.model_validate({
            "aoi": {"west": 106.3, "south": 29.3, "east": 106.8, "north": 29.8},
            "resolution_m": 9000,
        }))
        self.assertFalse(result["center_confirmed"])
        self.assertTrue(result["aoi_covered"])
        self.assertEqual(result["domains"][0]["dx"], 9000)

    def test_aoi_candidates_compare_grid_scale_without_auto_downgrade(self):
        result = assess(PlanningAssessRequest.model_validate({
            "goal": "降水", "aoi": {"west": 106.3, "south": 29.3, "east": 106.8, "north": 29.8},
            "resolution_m": 9000,
        }), {"status": "unknown"})
        self.assertEqual([item["finest_resolution_m"] for item in result["candidates"]],
                         [9000, 27000, 3000])
        self.assertFalse(result["candidates"][0]["center_confirmed"])
        self.assertTrue(all(item["selection_required"] for item in result["candidates"][1:]))
        self.assertIsNone(result["resource_estimate"]["runtime"])
        suggested = assess(PlanningAssessRequest.model_validate({
            "goal": "降水", "center": {"lat": 29.56, "lon": 106.55},
            "center_confirmed": False,
            "aoi": {"west": 106.3, "south": 29.3, "east": 106.8, "north": 29.8},
            "resolution_m": 9000,
        }), {"status": "unknown"})
        self.assertFalse(suggested["candidates"][0]["center_confirmed"])


if __name__ == "__main__":
    unittest.main()
