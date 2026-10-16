"""Execution-free regressions for the editable ordinary-ARW tree contract."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import f90nml
import numpy as np
from netCDF4 import Dataset
from pyproj import Transformer

from configuration_profile import PROFILE, LEGACY_PROFILE, definition, normalize
from domain_preview import DomainPreviewRequest, _projection, preview_domains
from guidance import recommendations
from namelist_config import resolve, domain_time_step, output_settings
from planning import PlanningAssessRequest, assess
from renderer import render_run
from schemas import Center, WrfTaskCreate, normalize_task_request
from test_namelist_config import request, config


def tree_request():
    result = {**request(), "configuration_profile": PROFILE}
    result["domains"] = [
        {"id":"d01","dx":27000,"dy":27000,"e_we":121,"e_sn":121,"parent_id":0},
        {"id":"d02","dx":9000,"dy":9000,"e_we":61,"e_sn":61,"parent_id":1,"parent_grid_ratio":3,"i_parent_start":11,"j_parent_start":11},
        {"id":"d03","dx":9000,"dy":9000,"e_we":61,"e_sn":61,"parent_id":1,"parent_grid_ratio":3,"i_parent_start":61,"j_parent_start":61},
        {"id":"d04","dx":3000,"dy":3000,"e_we":61,"e_sn":61,"parent_id":2,"parent_grid_ratio":3,"i_parent_start":11,"j_parent_start":11},
    ]
    return result


class OrdinaryUi2Test(unittest.TestCase):
    def test_options_empty_domain_and_editable_defaults(self):
        options = definition()
        self.assertEqual(options["id"], "wrf-ordinary-ui-2")
        self.assertEqual(options["defaults"]["domains"], [])
        self.assertEqual(options["defaults"]["spinup"]["hours"], 6)
        self.assertEqual(options["defaults"]["vertical_levels"], 88)
        self.assertEqual(options["geometry"]["topology"], "tree")
        self.assertEqual(options["geometry"]["default_domain"], "d01")
        self.assertEqual(options["physics_defaults"]["cu_physics"], 3)
        self.assertNotIn("vertical_levels", options["fixed_policy"])
        with self.assertRaises(ValueError):
            resolve({"configuration_profile":PROFILE, **options["defaults"]})

    def test_spinup_and_vertical_values_reach_both_namelists(self):
        for hours, levels in ((0,33),(3,60),(12,500)):
            value = tree_request()
            value.update(spinup={"mode":"custom","hours":hours},vertical_levels=levels)
            original = deepcopy(value)
            canonical = normalize_task_request(WrfTaskCreate.model_validate(value).model_dump(mode="json"))
            self.assertEqual(canonical["spinup"]["hours"],hours)
            self.assertEqual(canonical["vertical_levels"],levels)
            result = resolve(canonical)
            wps, wrf = (f90nml.reads(result["previews"][name]) for name in ("wps","input"))
            expected = datetime.fromisoformat(value["start_time"].replace("Z","+00:00")).timestamp()-hours*3600
            self.assertEqual(wps["share"]["start_date"][0],datetime.fromtimestamp(expected,timezone.utc).strftime("%Y-%m-%d_%H:%M:%S"))
            self.assertEqual(wrf["domains"]["e_vert"],[levels]*4)
            self.assertEqual(wrf["time_control"]["history_begin"],[hours*60]*4)
            self.assertEqual(output_settings(canonical,3)[1],hours*3600)
            self.assertEqual(value,original)

    def test_spinup_alignment_fdda_and_vertical_alias_guards(self):
        for patch in ({"spinup":{"hours":True}}, {"spinup":{"hours":-1}}, {"vertical_levels":True},
                      {"vertical_levels":9},{"vertical_levels":501}, {"spinup":{"mode":"automatic","hours":6}},
                      {"forecast_interval_hours":3,"spinup":{"hours":1}},
                      {"spinup":{"hours":0},"assimilation_scheme":"fdda_standard"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                WrfTaskCreate.model_validate({**tree_request(), **patch})
        value = {**tree_request(),"spinup":{"hours":3},"assimilation_scheme":"fdda_standard","vertical_levels":60}
        self.assertEqual(resolve(value)["effective"]["input"]["fdda"]["gfdda_end_h"],[3]*4)
        value["namelist"] = config(input={"fdda":{"gfdda_end_h":6}})
        with self.assertRaisesRegex(ValueError,"spin-up"):
            resolve(value)
        value["namelist"] = config(input={"domains":{"e_vert":[60,60,60,88]}})
        with self.assertRaisesRegex(ValueError,"e_vert"):
            resolve(value)

    def test_tree_namelist_parents_and_ancestor_steps(self):
        result = resolve(tree_request())
        wps, wrf = (f90nml.reads(result["previews"][name]) for name in ("wps","input"))
        self.assertEqual(wps["geogrid"]["parent_id"],[1,1,1,2])
        self.assertEqual(wrf["domains"]["parent_id"],[0,1,1,2])
        domain = {"parent_id":[0,1,1,2],"parent_time_step_ratio":[1,3,5,5],"time_step":120}
        self.assertEqual([domain_time_step(domain,i) for i in range(4)],[120,40,24,8])
        for bad in ([0,1,3,2],[0,4,1,2]):
            with self.assertRaises(ValueError):
                domain_time_step({**domain,"parent_id":bad},1 if bad[1] == 4 else 2)

    def test_tree_rejects_cycles_overlap_escape_and_false_ratio(self):
        edits = ((1,"parent_id",4),(1,"parent_id",2),(2,"parent_id",0),
                 (2,"i_parent_start",11),(1,"i_parent_start",115),(1,"parent_grid_ratio",5),
                 (3,"dx",4000),(3,"e_we",62))
        for index,key,new in edits:
            value = tree_request()
            value["domains"][index][key] = new
            if key == "i_parent_start" and index == 2:
                value["domains"][index]["j_parent_start"] = 11
            with self.subTest(index=index,key=key),self.assertRaises(ValueError):
                WrfTaskCreate.model_validate(value)
        value = tree_request()
        value["domains"].append(deepcopy(value["domains"][-1]))
        with self.assertRaises(ValueError):
            WrfTaskCreate.model_validate(value)

    def test_default_convection_and_explicit_choices_are_preserved(self):
        value = tree_request()
        self.assertEqual(normalize(value)["physics"]["cu_physics_by_domain"],[3,3,3,0])
        value["physics"]["cu_physics"] = 0
        self.assertEqual(resolve(value)["effective"]["input"]["physics"]["cu_physics"],[0]*4)
        value["physics"]["cu_physics_by_domain"] = [1,3,5,0]
        self.assertEqual(resolve(value,strict=False)["effective"]["input"]["physics"]["cu_physics"],[1,3,5,0])
        with self.assertRaisesRegex(ValueError,"跨域组合"):
            resolve(value)
        value["physics"].pop("cu_physics")
        self.assertEqual(WrfTaskCreate.model_validate(value).physics.cu_physics_by_domain,[1,3,5,0])

    def test_legacy_frozen_contract_does_not_inherit_new_defaults(self):
        value = {**request(),"configuration_profile":LEGACY_PROFILE,"namelist":config()}
        frozen = resolve(value)
        value["namelist"]["plan_digest"] = frozen["digest"]
        value.update(spinup={"hours":0},vertical_levels=60)
        checked = resolve(value)
        self.assertEqual(checked["digest"],frozen["digest"])
        self.assertEqual(checked["effective"]["input"]["domains"]["e_vert"],[88])
        self.assertEqual(checked["effective"]["input"]["time_control"]["history_begin"],[360])
        self.assertEqual(checked["configuration"]["profile_id"],LEGACY_PROFILE)
        with self.assertRaisesRegex(ValueError,"旧配置"):
            WrfTaskCreate.model_validate({**tree_request(),"configuration_profile":LEGACY_PROFILE})

    def test_tree_projection_origins_for_every_projection(self):
        value = tree_request()
        for projection in ("lambert","mercator","polar"):
            result = preview_domains(DomainPreviewRequest.model_validate({"center":value["center"],"domains":value["domains"],"configuration_profile":PROFILE,"projection":{"map_proj":projection}}))
            # Independent grid-coordinate arithmetic, without the production origin helper.
            root = -60*27000
            expected = [(root,root),(root+10*27000,root+10*27000),
                        (root+60*27000,root+60*27000),(root+10*27000+10*9000,root+10*27000+10*9000)]
            for domain,boundary,(x,y) in zip(value["domains"],result["boundaries"],expected):
                self.assertEqual(boundary["parent_id"],domain["parent_id"])
                self.assertEqual(boundary["projected_bounds_m"][:2],[x-domain["dx"]/2,y-domain["dy"]/2])

    def test_subtree_move_leaves_other_sibling_fixed(self):
        value = tree_request()
        payload = {"center":value["center"],"domains":value["domains"],"configuration_profile":PROFILE}
        original = deepcopy(payload)
        before = preview_domains(DomainPreviewRequest.model_validate(payload))
        center = Center.model_validate(value["center"])
        inverse = Transformer.from_crs(_projection(center),"EPSG:4326",always_xy=True)
        lon,lat = inverse.transform(27000,0)
        after = preview_domains(DomainPreviewRequest.model_validate({**payload,"edit":{"operation":"move","domain_id":"d02","start":value["center"],"end":{"lat":lat,"lon":lon}}}))
        self.assertEqual(before["boundaries"][2],after["boundaries"][2])
        for index in (1,3):
            self.assertAlmostEqual(after["boundaries"][index]["projected_bounds_m"][0]-before["boundaries"][index]["projected_bounds_m"][0],27000)
        self.assertEqual(payload,original)

    def test_resize_all_root_children_and_non_adjacent_grandchild(self):
        value = tree_request()
        for domain_id,handle,x,y,unchanged in (("d01","sw",-61*27000,-61*27000,(1,2,3)),
                                             ("d02","sw",-51*27000,-51*27000,(2,3))):
            payload = {"center":value["center"],"domains":value["domains"],"configuration_profile":PROFILE}
            before = preview_domains(DomainPreviewRequest.model_validate(payload))
            inverse = Transformer.from_crs(_projection(Center.model_validate(value["center"])),"EPSG:4326",always_xy=True)
            lon,lat = inverse.transform(x,y)
            after = preview_domains(DomainPreviewRequest.model_validate({**payload,"edit":{"operation":"resize","domain_id":domain_id,"handle":handle,"end":{"lat":lat,"lon":lon}}}))
            for index in unchanged:
                self.assertEqual(after["boundaries"][index]["corners"],before["boundaries"][index]["corners"])
                self.assertEqual(after["domains"][index]["dx"],before["domains"][index]["dx"])

    def test_aoi_coverage_uses_actual_finest_domain_not_last_sibling(self):
        value = tree_request()
        value["domains"][2],value["domains"][3] = value["domains"][3],value["domains"][2]
        value["domains"][2]["id"],value["domains"][3]["id"] = "d03","d04"
        payload = {"center":value["center"],"domains":value["domains"],"configuration_profile":PROFILE}
        before = preview_domains(DomainPreviewRequest.model_validate(payload))
        corners = before["boundaries"][2]["corners"]
        lon = sum(p[0] for p in corners.values())/4
        lat = sum(p[1] for p in corners.values())/4
        result = preview_domains(DomainPreviewRequest.model_validate({**payload,"aoi":{"west":lon-.01,"east":lon+.01,"south":lat-.01,"north":lat+.01}}))
        self.assertTrue(result["finest_aoi_covered"])

    def test_readonly_assessment_preserves_parameters_and_tree_workload(self):
        value = {**tree_request(),"spinup":{"hours":0},"vertical_levels":60,"goal":"冬季降雪"}
        result = assess(PlanningAssessRequest.model_validate(value),{"status":"unknown"})
        self.assertNotIn("center_confirmation",result["missing_fields"])
        self.assertEqual(result["suggestions"]["spinup"]["value"]["hours"],0)
        self.assertEqual(result["suggestions"]["vertical_levels"]["value"],60)
        self.assertNotIn("preset",result["suggestions"])
        weighted = (121*121+61*61*3+61*61*3+61*61*9)*60
        self.assertEqual(result["resources"]["ancestor_time_step_weighted_cells"],weighted)
        self.assertIsNone(result["resources"]["memory_gb"])
        self.assertTrue(result["candidates"])

    def test_missing_ui2_center_requests_coordinates_without_confirmation_gate(self):
        value = tree_request()
        value.pop("center")
        result = assess(PlanningAssessRequest.model_validate(value), {"status": "unknown"})
        self.assertIn("center", result["missing_fields"])
        self.assertNotIn("center_confirmation", result["missing_fields"])
        self.assertIn("提供计算域中心坐标", result["questions"][0])

    def test_rendering_prefers_d01_and_uses_selected_spinup(self):
        with TemporaryDirectory(prefix="wrf-ui2-render-") as workspace:
            raw = Path(workspace)/"raw"
            raw.mkdir()
            center = Center(lat=30,lon=105)
            inverse = Transformer.from_crs(_projection(center),"EPSG:4326",always_xy=True)
            x,y = np.meshgrid(np.arange(4)*9000,np.arange(3)*9000)
            lon,lat = inverse.transform(x,y)
            for domain in ("d01","d02"):
                with Dataset(str(raw/f"wrfout_{domain}_2026-09-30_06:00:00"),"w") as dataset:
                    for name,size in (("Time",1),("DateStrLen",19),("south_north",3),("west_east",4)):
                        dataset.createDimension(name,size)
                    for key,val in (("MAP_PROJ",1),("TRUELAT1",30.),("TRUELAT2",30.),("STAND_LON",105.),("DX",9000.),("DY",9000.)):
                        setattr(dataset,key,val)
                    dataset.createVariable("Times","S1",("Time","DateStrLen"))[0] = np.frombuffer(b"2026-09-30_06:00:00",dtype="S1")
                    for name,array in (("XLAT",lat),("XLONG",lon),("T2",np.full((3,4),280.))):
                        dataset.createVariable(name,"f8",("Time","south_north","west_east"))[0] = array
            value = {**request(),"spinup":{"hours":12},"end_time":"2026-09-30T07:00:00Z",
                     "domains":[{"id":"d01","dx":9000,"parent_id":0},{"id":"d02","dx":3000,"parent_id":1}]}
            result = render_run("fixture",raw,Path(workspace)/"output",value,"2026093000")
            self.assertEqual(result["default_domain"],"d01")
            self.assertEqual(result["default_variable"],"T2")
            self.assertEqual(result["domains"][1]["parent_id"],1)
            self.assertIn("2026-09-30T07:00:00Z",result["quality"]["missing_times"]["d01"])


if __name__ == "__main__":
    unittest.main()
