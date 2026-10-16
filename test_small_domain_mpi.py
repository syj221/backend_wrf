"""Keep geometry and match the pinned MPI runner."""
from copy import deepcopy
from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory
import subprocess
from unittest import TestCase
from unittest.mock import Mock, patch
from config import settings
from runtime_release import mpi_budget, require_ready, MpiConfigurationError
from database import TaskStore
from hpc import HpcClient


def request(x, y):
    return {'domains': [{'e_we': x, 'e_sn': y}], 'vertical_levels': 88}


class SmallDomainMpiTest(TestCase):
    def test_launch_passes_selected_rank_to_both_sources(self):
        with TemporaryDirectory() as directory:
            config = replace(settings, database_path=Path(directory)/'db.sqlite3',
                             hpc_release_root='/synthetic', hpc_workbench_data_root='/synthetic-data')
            store = TaskStore(config.database_path)
            self.addCleanup(store.close)
            task = 'wrf_gfs_20261014T000000Z_aaaaaaaa'
            store.create(task, request(10, 10), 'owner')
            store.update(task, runtime={'release_id': 'pinned', 'release_fingerprint': 'fixed'})
            client = HpcClient(config)
            for source in ('gfs', 'ecmwf'):
                client.run = Mock(side_effect=['', 'ready', '{"status":"running","process":{"pid":123}}'])
                with patch('runtime_release.require_ready', return_value={
                        'release_id': 'pinned', 'fingerprint': 'fixed',
                        'resource_budget': {'mpi_processes': 1}}):
                    client.launch(task, source, attempt='b'*32)
                commands = [call.args[0] for call in client.run.call_args_list]
                self.assertIn('WRF_CPU_MPI_PROCESSES=1', commands[1])
                self.assertIn('WRF_CPU_MPI_PROCESSES=1', commands[2])

    def test_geometry_and_shell_contract(self):
        script = Path('scripts/wrf.sh').read_text()
        function = script[script.index('select_safe_mpi_processes() {'):script.index('\nload_task_config()')]
        for x, y, expected in ((10, 10, 1), (19, 20, 1), (20, 20, 4), (100, 10, 1), (100, 100, 4)):
            value = request(x, y)
            original = deepcopy(value)
            self.assertEqual(mpi_budget(value, 4)['mpi_processes'], expected)
            self.assertEqual(value, original)
            harness = f'''domain_value() {{ if [ "$1" = WRF_E_WE ]; then echo {x}; else echo {y}; fi; }}
log_warn() {{ :; }}
{function}
select_safe_mpi_processes 4
'''
            result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, check=True)
            self.assertEqual(int(result.stdout), expected)
        value['domains'].append({'e_we': 10, 'e_sn': 10})
        self.assertEqual(mpi_budget(value, 4)['mpi_processes'], 1)

    def test_explicit_decomposition_and_actionable_error(self):
        value = request(10, 10)
        for nx, ny in ((-1, -1), (1, 1), (1, -1), (-1, 1)):
            value['namelist'] = {'input': {'domains': {'nproc_x': nx, 'nproc_y': ny}}}
            self.assertEqual(mpi_budget(value, 4)['mpi_processes'], 1)
        for nx, ny in ((2, 2), (0, -1), (3, -1)):
            value['namelist']['input']['domains'] = {'nproc_x': nx, 'nproc_y': ny}
            with self.assertRaisesRegex(MpiConfigurationError, '实际使用 1.*-1'):
                mpi_budget(value, 4)
        value = request(20, 20)
        value['namelist'] = {'input': {'domains': {'nproc_x': 4, 'nproc_y': 1}}}
        with self.assertRaisesRegex(MpiConfigurationError, 'D01.*5×20.*10'):
            mpi_budget(value, 4)

    def test_cache_and_memory_guard(self):
        report = {'status': 'ready', 'resources': {'cpu_count': 8, 'memory_available_bytes': 8 * 1024**3}}
        with patch('runtime_release.readiness', return_value=report):
            result = require_ready(Mock(), replace(settings,ordinary_max_concurrent=1), request=request(10, 10))
            self.assertEqual(result['resource_budget']['mpi_processes'], 1)
            self.assertNotIn('resource_budget', report)
            report['resources']['memory_available_bytes'] = 1
            with self.assertRaisesRegex(RuntimeError, '内存不足'):
                require_ready(Mock(), replace(settings,ordinary_max_concurrent=1), request=request(10, 10))

    def test_concurrent_submission_leaves_transient_memory_to_resource_queue(self):
        report={'status':'ready','resources':{'cpu_count':8,'memory_available_bytes':1}}
        with patch('runtime_release.readiness',return_value=report):
            result=require_ready(Mock(),replace(settings,ordinary_max_concurrent=4),request=request(10,10))
            self.assertEqual(result['resource_budget']['memory_bytes'],2*1024**3)
