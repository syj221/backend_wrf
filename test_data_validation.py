"""Fault-injection checks for decoded soil metadata and complete WRF output."""
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import os
import subprocess
import unittest

import netCDF4
import numpy as np

from scripts.validate_forcing import soil_layer
from scripts.validate_outputs import validate, REQUIRED
from scripts.output_schedule import history_settings, UNBOUNDED_SECONDS


class DataValidationTest(unittest.TestCase):
    def test_wrf_history_alarm_zero_end_and_unit_precedence(self):
        self.assertEqual(history_settings({'history_end': 0}, 0, 60, 360),
                         (3600, 21600, UNBOUNDED_SECONDS))
        self.assertEqual(history_settings({'history_interval': 30, 'history_interval_h': 1,
                                          'history_interval_m': 0, 'history_end_h': 8}, 0),
                         (5400, 0, 28800))
        self.assertEqual(history_settings({'history_interval': 60, 'history_interval_m': 15,
                                          'history_begin': [0, 180], 'history_end': [0, 360]}, 1),
                         (900, 10800, 21600))
        self.assertEqual(history_settings({'history_end_s': 90}, 0)[2], 90)
        with self.assertRaisesRegex(ValueError, 'negative_history_offset'):
            history_settings({'history_end': -1}, 0)

    def test_real_24h_spinup_schedule_checks_final_frame_with_zero_end(self):
        config={'time_range':{'product_start':'2026-10-06T06:00:00Z','model_start':'2026-10-06T00:00:00Z',
                'end_year':2026,'end_month':10,'end_day':7,'end_hour':6,'history_interval_minutes':60,'spinup_hours':6},
                'domains':[{'id':'d01','dx':27000,'dy':27000,'e_we':3,'e_sn':3}],
                'namelist_plan':{'previews':{'input':'&time_control\n history_interval=60, history_begin=360, history_end=0,\n/\n'}}}
        import datetime as dt
        with TemporaryDirectory() as directory:
            root=Path(directory)
            start=dt.datetime(2026,10,6,6)
            for index in range(24):
                stamp=(start+dt.timedelta(hours=index)).strftime('%Y-%m-%d_%H:%M:%S')
                self.write(root/str(index),stamp)
            report=validate(root,config)
            self.assertFalse(report['complete'])
            self.assertEqual(report['missing_times'],{'d01':['2026-10-07_06:00:00']})
            self.write(root/'24','2026-10-07_06:00:00')
            self.assertTrue(validate(root,config)['complete'])
            config['namelist_plan']['previews']['input']='&time_control\n history_interval=60, history_begin=360, history_end=420,\n/\n'
            # An explicitly limited alarm requires the two actual scheduled times.
            self.assertTrue(validate(root,config)['complete'])

    def test_zero_spinup_zero_end_still_requires_the_final_short_frame(self):
        config={'time_range':{'product_start':'2026-10-06T00:00:00Z','model_start':'2026-10-06T00:00:00Z',
                'end_year':2026,'end_month':10,'end_day':6,'end_hour':1,'history_interval_minutes':60,'spinup_hours':0},
                'domains':[{'id':'d01','dx':27000,'dy':27000,'e_we':3,'e_sn':3}],
                'namelist_plan':{'previews':{'input':'&time_control\n history_interval=60, history_begin=0, history_end=0,\n/\n'}}}
        with TemporaryDirectory() as directory:
            root=Path(directory);self.write(root/'zero','2026-10-06_00:00:00')
            self.assertEqual(validate(root,config)['missing_times'],{'d01':['2026-10-06_01:00:00']})

    def test_frozen_projection_topology_and_vertical_grid_are_required(self):
        config={'time_range':{'product_start':'2026-10-06T00:00:00Z','model_start':'2026-10-06T00:00:00Z',
                'end_year':2026,'end_month':10,'end_day':6,'end_hour':0,'history_interval_minutes':60,'spinup_hours':0},
                'center':{'lat':30,'lon':116},'vertical_levels':88,
                'domains':[{'id':'d01','dx':27000,'dy':27000,'e_we':3,'e_sn':3,'parent_id':0,'i_parent_start':1,'j_parent_start':1}],
                'namelist_plan':{'previews':{'wps':"&geogrid\n map_proj='lambert', truelat1=30, truelat2=30, stand_lon=116,\n/\n"}}}
        with TemporaryDirectory() as directory:
            root=Path(directory);self.write(root/'one','2026-10-06_00:00:00')
            path=root/'wrfout_d01_one'
            with netCDF4.Dataset(path,'a') as data:
                data.PARENT_ID=0;data.I_PARENT_START=1;data.J_PARENT_START=1;data.TRUELAT2=30.
                data.createDimension('bottom_top',87)
            self.assertTrue(validate(root,config)['complete'])
            for attr,bad,good,reason in (('MAP_PROJ',3,1,'projection_mismatch:MAP_PROJ'),
                    ('TRUELAT1',31.,30.,'projection_mismatch:TRUELAT1'),
                    ('STAND_LON',117.,116.,'projection_mismatch:STAND_LON'),
                    ('CEN_LAT',31.,30.,'domain_center_mismatch:CEN_LAT'),
                    ('PARENT_ID',1,0,'domain_topology_mismatch:PARENT_ID')):
                with self.subTest(attr=attr):
                    with netCDF4.Dataset(path,'a') as data: setattr(data,attr,bad)
                    self.assertIn(reason,validate(root,config)['invalid'][0]['reason'])
                    with netCDF4.Dataset(path,'a') as data: setattr(data,attr,good)
            config['vertical_levels']=60
            self.assertIn('vertical_grid_mismatch',validate(root,config)['invalid'][0]['reason'])

    def test_ecmwf_task_index_is_mandatory_and_checks_its_own_hash(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);cycle=root/'2026100600';cycle.mkdir()
            name='ecmwf.t00z.ifs.0p25.f000.grib2';data=cycle/name;data.write_bytes(b'index-only-fixture')
            index=root/'expected.tsv'
            base={**os.environ,'WRF_NONINTERACTIVE':'true','WORK_DIR':directory,'WRF_TASK_TAG':'fixture',
                  'WRF_ATTEMPT_DIR':directory,'WRF_EC_CACHE_ROOT':directory,'WRF_EC_DATE':'20261006',
                  'WRF_EC_HOUR':'00','WRF_EC_FORECAST_HOURS':'0','WRF_EC_EXPECTED_INDEX':str(index),
                  'WRF_GFS_EXPECTED_INDEX':''}
            script=Path(__file__).parent/'scripts/wrf.sh'
            command=['bash','-c','source "$1"; step1_forecast_data_preparation ec','fixture',str(script)]
            for digest,expected in ((hashlib.sha256(data.read_bytes()).hexdigest(),0),('0'*64,1)):
                index.write_text(f'{name}\t{data.stat().st_size}\t{digest}\n')
                result=subprocess.run(command,env=base,capture_output=True,text=True)
                self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
            index.unlink()
            self.assertNotEqual(subprocess.run(command,env=base,capture_output=True).returncode,0)

    def test_fractional_gfs_layers_and_numeric_ecmwf_layers(self):
        for level_type, pairs, scale, unit in (
            ('depthBelowLandLayer', [(0,10),(10,40),(40,100),(100,200)],2,'metres'),
            ('soilLayer',[(0,1),(1,2),(2,3),(3,4)],0,'index'),
        ):
            decoded=set()
            for top,bottom in pairs:
                fields={'scaledValueOfFirstFixedSurface':top,'scaledValueOfSecondFixedSurface':bottom,
                        'scaleFactorOfFirstFixedSurface':scale,'scaleFactorOfSecondFixedSurface':scale}
                decoded.add(soil_layer(fields.get,level_type))
            self.assertEqual(len(decoded),4)
            self.assertTrue(all(item[0]==unit for item in decoded))
        self.assertIsNone(soil_layer(lambda key:None,'soilLayer'))

    def test_domain_schedule_missing_frame_and_nonfinite_output(self):
        config={'time_range':{'product_start':'2026-10-06T00:00:00Z','model_start':'2026-10-06T00:00:00Z',
                'end_year':2026,'end_month':10,'end_day':6,'end_hour':1,'history_interval_minutes':60,'spinup_hours':0},
                'domains':[{'id':'d01','dx':27000,'dy':27000,'e_we':3,'e_sn':3}],
                'namelist_plan':{'previews':{'input':'&time_control\n history_interval=30, history_begin=0,\n/\n'}}}
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for index,stamp in enumerate(('2026-10-06_00:00:00','2026-10-06_01:00:00')):
                self.write(root/index.__str__(),stamp)
            report=validate(root,config)
            self.assertFalse(report['complete'])
            self.assertEqual(report['missing_times']['d01'],['2026-10-06_00:30:00'])
            self.write(root/'third','2026-10-06_00:30:00')
            self.assertTrue(validate(root,config)['complete'])
            self.write(root/'third','2026-10-06_00:30:00',nan=True)
            report=validate(root,config)
            self.assertFalse(report['complete'])
            self.assertTrue(any('non_finite_values:T2' in item['reason'] for item in report['invalid']))

    @staticmethod
    def write(path,stamp,nan=False):
        path=path.with_name('wrfout_d01_'+path.name)
        with netCDF4.Dataset(path,'w') as data:
            for name,size in (('Time',1),('DateStrLen',19),('west_east',2),('south_north',2)):
                data.createDimension(name,size)
            data.GRID_ID=1;data.MAP_PROJ=1;data.DX=27000.;data.DY=27000.
            data.CEN_LAT=30.;data.CEN_LON=116.;data.TRUELAT1=30.;data.STAND_LON=116.
            data.createVariable('Times','S1',('Time','DateStrLen'))[:]=np.array(list(stamp),'S1')[None,:]
            for name in REQUIRED[1:]:
                data.createVariable(name,'f4',('Time','south_north','west_east'))[:]=np.nan if nan and name=='T2' else 1.


if __name__=='__main__': unittest.main()
