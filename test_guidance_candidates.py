"""Candidate evidence never executes or silently repairs a user configuration."""
import unittest
from copy import deepcopy
from guidance_candidates import candidates
from planning import PlanningAssessRequest, assess
from test_ordinary_ui2 import tree_request
from test_namelist_config import config


class GuidanceCandidatesTest(unittest.TestCase):
    def items(self, request):
        return {item["id"]: item for item in candidates(request, {})["items"]}

    def test_all_topics_and_immutable_request(self):
        request = tree_request()
        before = deepcopy(request)
        result = self.items(request)
        self.assertEqual(len(result), 7)
        self.assertEqual(request, before)
        self.assertTrue(all(c["validation_status"] == "valid" for item in result.values() for c in item["candidates"]))
        self.assertTrue(result["data_source"]["candidates"])

    def test_ecmwf_only_three_hour_interval_and_preserve_times(self):
        request = {**tree_request(), "data_source": "ecmwf", "forecast_interval_hours": 3}
        options = self.items(request)["forecast_interval_hours"]["candidates"]
        self.assertEqual([x["patch"]["forecast_interval_hours"] for x in options], [3])
        self.assertTrue(all("start_time" not in x["patch"] for x in options))

    def test_no_spinup_disables_fdda_candidates(self):
        result = self.items({**tree_request(), "spinup": {"mode": "custom", "hours": 0}})
        self.assertEqual([c["patch"]["assimilation_scheme"] for c in result["assimilation_scheme"]["candidates"]], ["off"])

    def test_advanced_namelist_reaches_candidate_validation(self):
        request = tree_request()
        request["namelist"] = config(input={"domains": {"e_vert": [60]*4}})
        payload = PlanningAssessRequest.model_validate(request)
        self.assertEqual(payload.namelist, request["namelist"])
        self.assertFalse(self.items(payload.model_dump(mode="json", exclude_none=True))["vertical_levels"]["candidates"])

    def test_physics_preserves_per_domain_overrides(self):
        request = tree_request()
        request["physics"]["cu_physics_by_domain"] = [3, 3, 3, 0]
        before = deepcopy(request)
        self.assertEqual(self.items(request)["physics"]["candidates"][0]["patch"], {})
        self.assertEqual(request, before)

    def test_partial_candidates_not_mislabelled_as_valid(self):
        result = self.items({"configuration_profile": "wrf-ordinary-ui-2"})
        self.assertTrue(all(c["validation_status"] == "requires_configuration" for item in result.values() for c in item["candidates"]))

    def test_assess_compatible_response(self):
        result = assess(PlanningAssessRequest.model_validate(tree_request()), {})
        self.assertIn("suggestions", result)
        self.assertEqual(result["guidance"]["schema"], "wrf-guidance-1")
        self.assertEqual(result["guidance"]["items"], [])
        self.assertTrue(result["guidance"]["retired"])
