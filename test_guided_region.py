"""Read-only geographic and future-window suggestions."""
import unittest
from datetime import datetime, timezone
from pyproj import Geod
from domain_preview import DomainPreviewRequest, preview_domains
from guidance import recommendations

class GuidedRegionTest(unittest.TestCase):
    def test_kunming_cardinal_extents_and_three_resolution_coverage(self):
        geod = Geod(ellps='WGS84')
        for spacing in (27000,9000,3000):
            value=preview_domains(DomainPreviewRequest(center={'lat':25.04,'lon':102.71},region_half_extent_km=100,resolution_m=spacing,configuration_profile='wrf-ordinary-ui-2'))
            self.assertTrue(value['finest_aoi_covered'])
            for azimuth,key,coordinate in ((270,'west',0),(90,'east',0),(180,'south',1),(0,'north',1)):
                self.assertAlmostEqual(value['aoi'][key],geod.fwd(102.71,25.04,azimuth,100000)[coordinate],places=8)
    def test_location_extent_does_not_reject_smaller_explicit_domain(self):
        value = preview_domains(DomainPreviewRequest(
            center={'lat':41.8057,'lon':123.4315},
            domains=[{'id':'d01','dx':27000,'dy':27000,'e_we':11,'e_sn':11,
                      'parent_id':0,'parent_grid_ratio':1,'i_parent_start':1,'j_parent_start':1},
                     {'id':'d02','dx':9000,'dy':9000,'e_we':13,'e_sn':16,
                      'parent_id':1,'parent_grid_ratio':3,'i_parent_start':4,'j_parent_start':3}],
            aoi={'west':122.23,'east':124.63,'south':40.91,'north':42.71},
            configuration_profile='wrf-ordinary-ui-2'))
        self.assertFalse(value['finest_aoi_covered'])
        self.assertEqual(value['domains'][1]['e_we'],13)
        self.assertFalse(any('关注区域' in text for text in value['warnings']))

    def test_suggestion_cannot_replace_explicit_geometry(self):
        for extra in ({'aoi':{'west':102,'east':103,'south':24,'north':26}},{'domains':[]}):
            with self.assertRaises(ValueError):
                DomainPreviewRequest(center={'lat':25.04,'lon':102.71},region_half_extent_km=100,**extra)
    def test_future_candidates_preserve_six_hours(self):
        now=datetime(2026,10,15,2,17,tzinfo=timezone.utc)
        request={'duration_hours':6,'spinup':{'mode':'custom','hours':0},'configuration_profile':'wrf-ordinary-ui-2'}
        options=recommendations(request,{},now=now)['suggestions']['time']['candidates']
        self.assertEqual(len(options),3)
        for option in options:
            start=datetime.fromisoformat(option['value']['start_time'].replace('Z','+00:00'))
            end=datetime.fromisoformat(option['value']['end_time'].replace('Z','+00:00'))
            self.assertGreaterEqual(start,now)
            self.assertEqual((end-start).total_seconds(),6*3600)
        self.assertNotIn('candidates',recommendations({**request,**options[1]['value']},{},now=now)['suggestions']['time'])
