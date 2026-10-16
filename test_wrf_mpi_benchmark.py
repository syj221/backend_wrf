import unittest
import f90nml
from scripts.wrf_mpi_benchmark import short_namelist


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_only_changes_time_control(self):
        original = '''&time_control
 run_days=0, run_hours=6, run_minutes=0, run_seconds=0,
 start_year=2026, start_month=9, start_day=18, start_hour=12,
 start_minute=0, start_second=0, end_year=2026, end_month=9,
 end_day=18, end_hour=18, end_minute=0, end_second=0,
 history_interval=60,
/
&domains
 max_dom=1, e_we=61, e_sn=61, e_vert=88, dx=27000, dy=27000,
/
&physics
 mp_physics=8, cu_physics=3,
/'''
        before, after = f90nml.reads(original), f90nml.reads(short_namelist(original))
        for group in before:
            if group != 'time_control':
                self.assertEqual(before[group], after[group])
        self.assertEqual(after['time_control']['run_seconds'], 8)
        self.assertEqual(after['time_control']['end_hour'], 12)
        self.assertEqual(after['time_control']['end_second'], 8)
        self.assertEqual(after['time_control']['history_interval_s'], 8)


if __name__ == '__main__':
    unittest.main()
