"""Regression evidence for deterministic initialization vs input-dependent checks."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from namelist_config import resolve
from request_validation import validate_ordinary_request
from test_namelist_config import request, config
from vertical_validation import automatic_levels, old_levels, validate_vertical
from guidance import configuration_rules


class VerticalChecks(unittest.TestCase):
    def test_pinned_fortran_reference_matrix(self):
        reference=json.loads((Path(__file__).parent/'test_fixtures/vertical_fortran_reference.json').read_text())
        self.assertEqual(len(reference['cases']),65)
        for case in reference['cases']:
            mode,n,top,maximum,bottom,surface,upper=case['args']
            with self.subTest(args=case['args']):
                result=automatic_levels(n,top,maximum,bottom,surface,upper) if mode==2 else old_levels(n,top,maximum,{})
                self.assertEqual(result[0] is None,case['ready'])
                if result[0]=='unreachable':self.assertIn('not enough eta levels',case['fatal'])
                if result[0]=='thick':self.assertEqual(case['fatal'],'Upper levels may be too thick')

    def test_failed_twenty_layer_combination_is_blocked_without_mutation(self):
        payload=request();payload['namelist']=config(input={'domains':{'e_vert':20}})
        original=deepcopy(payload)
        self.assertIn('至少设为 33',resolve(payload,strict=False)['errors'][0])
        with self.assertRaisesRegex(ValueError,'not enough eta levels'):
            resolve(payload)
        self.assertEqual(payload,original)

    def test_twenty_layers_are_not_arbitrarily_banned(self):
        for fields in ({'p_top_requested':80000}, {'eta_levels':[1-i/19 for i in range(20)]},
                       {'max_dz':2000,'dzbot':100,'dzstretch_s':1.5,'dzstretch_u':1.2}):
            payload=request();payload['namelist']=config(input={'domains':{'e_vert':20,**fields}})
            with self.subTest(fields=fields):self.assertEqual(resolve(payload)['errors'],[])

    def test_legacy_validation_cannot_bypass_vertical_checks(self):
        payload=request();payload['vertical_levels']=20;payload['configuration_profile']='wrf-ordinary-ui-2'
        with self.assertRaisesRegex(ValueError,'not enough eta levels'):
            validate_ordinary_request(payload)
        payload.pop('configuration_profile');payload['namelist']=config(input={'domains':{'e_vert':20}})
        with self.assertRaisesRegex(ValueError,'not enough eta levels'):
            validate_ordinary_request(payload)

    def test_auto_parameters_and_pressure_boundaries(self):
        for key,value in [('p_top_requested',0),('p_top_requested',100000),('p_top_requested',100001),
                          ('auto_levels_opt',3),('max_dz',0),('dzbot',-1),('dzstretch_s',0),('dzstretch_u',-1)]:
            with self.subTest(key=key,value=value):
                self.assertTrue(validate_vertical({'e_vert':[88],key:value},{}))
        self.assertIn('至少 12',validate_vertical({'e_vert':[10],'auto_levels_opt':1},{} )[0])

    def test_explicit_eta_single_precision_and_length(self):
        for eta in ([1,.5,0], [1]+[.5]*18+[0], [1,.999999999]+[1-i/18 for i in range(1,18)]+[0]):
            with self.subTest(eta=eta):self.assertTrue(validate_vertical({'e_vert':[20],'eta_levels':eta},{}))

    def test_thickness_and_old_algorithm_errors_are_specific(self):
        self.assertIn('顶部层厚',validate_vertical({'e_vert':[20],'max_dz':200,'dzbot':1000},{} )[0])
        self.assertIn('顶部层厚',validate_vertical({'e_vert':[20],'auto_levels_opt':1},{} )[0])

    def test_pending_conditions_are_explicit_not_promises(self):
        result=resolve(request())
        pending=[r for r in result['rules'] if r['severity']=='pending']
        self.assertEqual(len(pending),3)
        self.assertTrue(all(r['category']=='input_dependent' and '待核验' in r['message'] for r in pending))
        self.assertIn('实际驱动资料最高层气压',pending[0]['message'])


class OtherCombinations(unittest.TestCase):
    def test_deterministic_pairs_fail_before_initialization(self):
        pairs=[('physics',{'sf_surface_physics':3},'需要 6 层土壤'),
               ('physics',{'qna_update':1},'auxinput17'),
               ('physics',{'aercu_opt':1},'mp_physics=40'),
               ('physics',{'mp_physics':28,'use_rap_aero_icbc':True},'aer_init_opt>1'),
               ('dynamics',{'km_opt':5,'diff_opt':1},'diff_opt=2'),
               ('dynamics',{'km_opt':5,'diff_opt':2},'bl_pbl_physics=0'),
               ('dynamics',{'base_lapse':0},'除零'),
               ('domains',{'vert_refine_method':1},'垂直嵌套'),
               ('bdy_control',{'spec_zone':2},'spec_zone=1'),
               ('bdy_control',{'spec_bdy_width':0},'spec_bdy_width'),
               ('physics',{'sf_surface_mosaic':1,'sf_urban_physics':2},'城市冠层'),
               ('physics',{'shcu_physics':5},'MYJ'),
               ('physics',{'icloud':3,'mp_physics':1},'QICE'),
               ('physics',{'sf_surf_irr_scheme':2,'irr_num_hours':0},'irr_num_hours'),
               ('physics',{'cu_diag':1,'cu_physics':0},'cu_diag'),
               ('physics',{'kf_edrates':1,'cu_physics':3},'kf_edrates'),
               ('physics',{'tmn_update':1,'lagday':0},'lagday'),
               ('physics',{'ra_sw_eclipse':1,'ra_sw_physics':3},'短波'),
               ('physics',{'couple_farms':True,'swint_opt':1},'swint_opt')]
        for group,fields,message in pairs:
            payload=request();payload['namelist']=config(input={group:fields});original=deepcopy(payload)
            with self.subTest(group=group,fields=fields):
                self.assertIn(message,'；'.join(resolve(payload,strict=False)['errors']))
                with self.assertRaises(ValueError):resolve(payload)
                self.assertEqual(payload,original)

    def test_integer_overflow_and_enabled_input_dependencies(self):
        payload=request();payload['namelist']=config(input={'time_control':{'frames_per_outfile':2**31}})
        with self.assertRaisesRegex(ValueError,'32 位整数'):
            resolve(payload)
        payload['namelist']=config(input={'physics':{'sf_urban_physics':1}})
        result=resolve(payload)
        self.assertTrue(any(r['id']=='runtime.sf_urban_physics_pending' for r in result['rules']))
        self.assertEqual(result['errors'],[])

    def test_output_window_fractional_step_and_mpi_conflicts(self):
        for fields,expected in (({'time_step_fract_num':1},'整数秒'),
                                ({'nproc_x':3,'nproc_y':1},'MPI 分解参数不匹配')):
            payload=request();payload['namelist']=config(input={'domains':fields})
            with self.subTest(fields=fields),self.assertRaisesRegex(ValueError,expected):
                resolve(payload)
        payload=request();payload['namelist']=config(input={'time_control':{'history_begin':2000}})
        with self.assertRaisesRegex(ValueError,'将无结果'):
            resolve(payload)
        payload=request();payload['domains'][0].update(e_we=10,e_sn=10)
        self.assertEqual(resolve(payload)['resource_budget']['mpi_processes'],1)

    def test_cross_domain_physics_retains_choices_and_blocks_conflict(self):
        payload=request();payload['domains'].append({'id':'d02','dx':3000,'dy':3000,'e_we':61,'e_sn':61,'parent_id':1,'parent_grid_ratio':3,'i_parent_start':11,'j_parent_start':11})
        payload['namelist']=config(input={'physics':{'cu_physics':[3,1]}})
        result=resolve(payload,strict=False)
        self.assertEqual(result['effective']['input']['physics']['cu_physics'],[3,1])
        self.assertIn('跨域组合','；'.join(result['errors']))
        for key in ('mp_physics','sf_urban_physics'):
            altered=deepcopy(payload)
            altered['namelist']=config(input={'physics':{key:[8,6] if key=='mp_physics' else [1,0]}})
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'跨域组合'):
                resolve(altered)
        payload['namelist']['input']['physics']['cu_physics']=[3,0]
        self.assertEqual(resolve(payload)['errors'],[])

if __name__=='__main__':unittest.main()
