"""Compute-free common-contract, projection and resize regressions."""
from copy import deepcopy
import unittest
import f90nml
from pyproj import Transformer

from configuration_profile import PROFILE, definition, normalize
from domain_preview import DomainPreviewRequest, preview_domains, _projection
from namelist_config import resolve
from schemas import Center
from schemas import WrfTaskCreate
from request_validation import validate_ordinary_request
from test_recovery_diagnosis import runtime
from test_namelist_config import request, config


class CommonConfigurationTest(unittest.TestCase):
    def common(self):
        return {**request(), "configuration_profile":PROFILE,"namelist":config()}

    def test_six_groups_not_registry_count_and_internal_defaults(self):
        r=self.common()
        for surface,fdda in ((2,'off'),(4,'fdda_standard')):
            r['physics']['sf_surface_physics']=surface;r['assimilation_scheme']=fdda
            result=resolve(r)
            self.assertEqual(len(result['confirmation_groups']),6)
            self.assertGreater(len(result['required_confirmation']),200)
            self.assertEqual(result['effective']['input']['domains']['e_vert'],[88])
            self.assertNotIn('vertical_levels',result['configuration']['fixed_policy'])
            n=f90nml.reads(result['previews']['input'])
            self.assertEqual(n['physics']['sf_surface_physics'],surface)
            if fdda == 'off': self.assertNotIn('fdda',n)
            else: self.assertEqual(n['fdda']['grid_fdda'],1)

    def test_alias_conflict_and_extra_override(self):
        r=self.common();r['namelist']=config(input={'physics':{'mp_physics':6}})
        with self.assertRaisesRegex(ValueError,'不同值'):resolve(r)
        r['physics']['mp_physics']=6;r['namelist']['input']['physics']['mp_physics']=[6]
        self.assertNotIn('mp_physics',normalize(r)['namelist']['input']['physics'])
        r['namelist']=config(input={'dynamics':{'diff_opt':1}})
        self.assertEqual(resolve(r)['configuration']['extra_overrides']['input']['dynamics']['diff_opt'],1)
        r['namelist']=config(input={'domains':{'e_vert':60}})
        with self.assertRaisesRegex(ValueError,'88'):resolve(r)

    def test_options_exposed_fields_and_old_requests(self):
        self.assertEqual(len(definition()['physics_fields']),11)
        self.assertNotIn('confirmation_groups',resolve(request()))
        self.assertEqual(normalize(request()),request())
        r=self.common();r.update(data_source='ecmwf',forecast_interval_hours=1)
        with self.assertRaises(ValueError):normalize(r)

    def test_optional_namelist_and_parameter_sources(self):
        r=self.common();r['namelist']=None;r['physics'].pop('mp_physics')
        resolved=resolve(r)
        self.assertEqual(resolved['configuration']['parameter_sources']['physics']['mp_physics'],'service_default')
        self.assertEqual(resolved['configuration']['parameter_sources']['physics']['bl_pbl_physics'],'request')
        self.assertEqual(resolved['configuration']['parameter_sources']['projection']['map_proj'],'derived_from_center')
        self.assertEqual(len(resolved['confirmation_groups']),6)
        r['physics']['mp_physics']=True
        with self.assertRaisesRegex(ValueError,'整数'):resolve(r)

    def test_http_typed_defaults_keep_original_field_presence_outside_digest(self):
        sparse=self.common();sparse.pop('physics');sparse.pop('forecast_interval_hours')
        sparse['spinup']={'hours':0};sparse['vertical_levels']=60
        payload=WrfTaskCreate.model_validate(sparse)
        dumped=payload.model_dump(mode='json')
        self.assertNotIn('_provided_request',dumped)
        result=validate_ordinary_request(dumped,runtime=runtime(),original_request=payload._provided_request)
        sources=result['namelist_plan']['configuration']['parameter_sources']
        self.assertEqual(sources['physics']['mp_physics'],'service_default')
        self.assertEqual(sources['forcing']['forecast_interval_hours'],'service_default')
        self.assertEqual(sources['spinup_detail'],{'mode':'service_default','hours':'request'})
        self.assertEqual(sources['domains']['vertical_levels'],'request')
        self.assertEqual(sources['center_detail']['lat'],'request')
        self.assertEqual(sources['physics_by_domain']['cu_physics'],['service_default'])
        ordinary=validate_ordinary_request(dumped,runtime=runtime())
        self.assertEqual(result['request'],ordinary['request'])
        self.assertEqual(result['namelist_plan']['digest'],ordinary['namelist_plan']['digest'])
        self.assertEqual(result['namelist_plan']['previews'],ordinary['namelist_plan']['previews'])

    def test_group_candidates_reject_wrong_types_and_time_alignment(self):
        for key,value in [('forecast_interval_hours',0),('history_interval_minutes',True),('domains',['invalid'])]:
            r=self.common();r[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):resolve(r)
        r=self.common();r['forecast_interval_hours']=3;r['start_time']='2026-09-30T07:00:00Z'
        with self.assertRaisesRegex(ValueError,'对齐'):resolve(r)
        r=self.common();r['namelist']=config(input={'physics':{'cu_physics':False}})
        with self.assertRaisesRegex(ValueError,'整数'):resolve(r)

    def test_common_geometry_limits_match_preview_without_rewriting_legacy(self):
        for spacing in (900,51000):
            r=self.common();r['domains'][0].update(dx=spacing,dy=spacing)
            with self.assertRaisesRegex(ValueError,'普通配置服务'):resolve(r)
            p={'center':r['center'],'domains':r['domains'],'configuration_profile':PROFILE}
            with self.assertRaisesRegex(ValueError,'普通配置服务'):preview_domains(DomainPreviewRequest.model_validate(p))
            # Legacy contracts are not retrospectively narrowed.
            del r['configuration_profile']
            self.assertEqual(normalize(r),r)
        r=self.common();r['domains'][0]['dx']='9000'
        with self.assertRaisesRegex(ValueError,'整数米'):resolve(r)

    def test_equivalent_common_inputs_produce_identical_namelists(self):
        first=self.common();first['namelist']=config(input={'dynamics':{'diff_opt':1}})
        second=deepcopy(first)
        second['namelist']['input']['physics']={'mp_physics':[8]}
        second['history_interval_minutes']=60
        second['namelist']['input']['time_control']={'history_interval':[60]}
        a,b=resolve(first),resolve(second)
        self.assertEqual(a['effective'],b['effective'])
        self.assertEqual(a['previews'],b['previews'])

    def test_resize_keeps_child_position_and_grid_resolution(self):
        for name in ('lambert','mercator','polar'):
            r=request();r['domains'].append({'id':'d02','parent_id':1,'dx':3000,'dy':3000,'e_we':61,'e_sn':61,'parent_grid_ratio':3,'i_parent_start':20,'j_parent_start':20})
            payload={'center':r['center'],'domains':r['domains'],'projection':{'map_proj':name,'truelat1':32.06,'truelat2':32.06,'stand_lon':118.79}}
            before=preview_domains(DomainPreviewRequest.model_validate(payload))
            transform=Transformer.from_crs(_projection(Center.model_validate(r['center']),payload['projection']),'EPSG:4326',always_xy=True)
            lon,lat=transform.transform(-55.5*9000,-55.5*9000)
            after=preview_domains(DomainPreviewRequest.model_validate({**payload,'edit':{'operation':'resize','domain_id':'d01','handle':'sw','end':{'lat':lat,'lon':lon}}}))
            self.assertEqual(after['boundaries'][1]['corners'],before['boundaries'][1]['corners'])
            self.assertEqual(after['domains'][1]['dx'],3000)
            self.assertGreater(after['domains'][0]['e_we'],before['domains'][0]['e_we'])
            lon,lat=transform.transform(40*9000,40*9000)
            with self.assertRaises(ValueError):preview_domains(DomainPreviewRequest.model_validate({**payload,'edit':{'operation':'resize','domain_id':'d01','handle':'sw','end':{'lat':lat,'lon':lon}}}))

    def test_grid_alignment_and_preview_namelist_projection_match(self):
        for count in range(1,5):
            for ratio in (3,5):
                # The preview's requested finest spacing is at least1km; construct valid integer grids directly.
                spacing=ratio**(count-1)*1000
                if spacing>50000:continue
                ds=[{'id':f'd{i+1:02}','parent_id':i,'dx':spacing//ratio**i,'dy':spacing//ratio**i,'e_we':101 if i==0 else 1+30*ratio,'e_sn':101 if i==0 else 1+30*ratio,'parent_grid_ratio':ratio if i else 1,'i_parent_start':11 if i else 1,'j_parent_start':11 if i else 1} for i in range(count)]
                for name in ('lambert','mercator','polar'):
                    p=preview_domains(DomainPreviewRequest.model_validate({'center':request()['center'],'domains':ds,'projection':{'map_proj':name}}))
                    r=self.common();r.update(center=p['center'],domains=p['domains']);r['namelist']['wps']={'geogrid':p['projection_parameters']}
                    resolved=resolve(r);n=f90nml.reads(resolved['previews']['wps'])
                    self.assertEqual(n['geogrid']['map_proj'],p['projection_parameters']['map_proj'])
                    self.assertEqual(len(p['boundaries']),count)


if __name__=='__main__':unittest.main()
