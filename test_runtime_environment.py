"""Ordinary execution invariants without SSH, live databases or computation."""
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from config import settings
from database import TaskStore
from hpc import build_task_config, _runtime_environment
from hpc_transport import HpcClient, HpcError
from runtime_release import readiness, require_ready, _cache
from scripts import managed_process as process
from task_manager import WrfTaskManager, resolve_spinup_hours
from test_ordinary_ui2 import tree_request


class RuntimeEnvironmentTest(TestCase):
    def test_supervisor_binding_preserves_compute_release_and_checks_fingerprint(self):
        client = HpcClient(replace(settings,hpc_release_root='/fixed'))
        runtime={'release_id':'old','supervisor_release_id':'new','supervisor_fingerprint':'known'}
        with patch.object(client,'_task_runtime',return_value=runtime), patch('runtime_release.require_ready',return_value={'fingerprint':'known'}) as ready:
            self.assertEqual(client.service_dir('task'),'/fixed/releases/old/scripts')
            self.assertEqual(client.supervisor_script('task',verify=True),'/fixed/releases/new/scripts/managed_process.py')
            self.assertEqual(ready.call_args.args[1].hpc_runtime_release,'new')
            ready.return_value={'fingerprint':'changed'}
            with self.assertRaisesRegex(HpcError,'fingerprint changed'):client.supervisor_script('task',verify=True)
        with patch.object(client,'_task_runtime',return_value={'release_id':'old'}):
            self.assertEqual(client.supervisor_script('task'),'/fixed/releases/old/scripts/managed_process.py')

    def test_bound_output_validator_uses_operator_binding_without_relaunch(self):
        client = HpcClient(settings)
        task_id = 'wrf_ecmwf_20261006T060000Z_3134eb5a'
        task = {'id': task_id}
        report = {'schema': 'wrf-output-validation-1', 'complete': True}
        with patch.object(client, '_task_runtime', return_value={'output_validation_override': {}}), \
                patch('output_validation_recovery.load_bound_task', return_value=task) as load, \
                patch('output_validation_recovery.validate_bound_outputs', return_value=report) as validate, \
                patch.object(client, 'run') as remote:
            self.assertEqual(client.validate_outputs(task_id), report)
            load.assert_called_once_with(settings.database_path, task_id,
                config_path=settings.run_dir / task_id / 'task.config.json')
            validate.assert_called_once_with(client, settings, task)
            remote.assert_not_called()

    def test_invalid_output_override_cannot_fall_back_to_old_validator(self):
        client = HpcClient(settings)
        with patch.object(client, '_task_runtime', return_value={'output_validation_override': {'schema': 'invalid'}}), \
                patch('output_validation_recovery.load_bound_task', side_effect=ValueError('binding mismatch')), \
                patch.object(client, 'run') as remote:
            with self.assertRaisesRegex(ValueError, 'binding mismatch'):
                client.validate_outputs('wrf_ecmwf_20261006T060000Z_3134eb5a')
            remote.assert_not_called()

    def test_config_and_runtime_receive_editable_values_and_tree(self):
        value = {**tree_request(), 'spinup': {'mode':'custom','hours':0},'vertical_levels':55}
        config = build_task_config('wrf_gfs_20261008T000000Z_aaaaaaaa', value, '2026100800', [0,1])
        env = _runtime_environment(config)
        self.assertEqual(config['time_range']['spinup_hours'],0)
        self.assertEqual(env['WRF_E_VERT'],'55')
        self.assertEqual(env['WRF_HISTORY_BEGIN_MINUTES'],'0')
        self.assertEqual(env['WRF_PARENT_ID_D03'],'1')
        self.assertEqual(env['WRF_PARENT_ID_D04'],'2')
        self.assertEqual(resolve_spinup_hours(value),0)

    def test_unconfigured_environment_blocks_without_network(self):
        _cache.clear(); client = Mock()
        config = replace(settings,hpc_release_root='',hpc_runtime_release='',hpc_workbench_data_root='')
        report = readiness(client,config,force=True)
        self.assertEqual(report['status'],'blocked')
        client.run.assert_not_called()
        with self.assertRaises(RuntimeError): require_ready(client,config)

    def test_serial_reservation_survives_api_restart_and_unknown_state(self):
        with TemporaryDirectory() as directory:
            config = replace(settings,database_path=Path(directory)/'tasks.sqlite3',ordinary_max_concurrent=1)
            store = TaskStore(config.database_path)
            self.addCleanup(store.close)
            store.create('one', {}, 'owner'); store.create('two',{},'owner')
            store.update('one',status='paused_external',runtime={'remote_launch_attempted':True})
            manager = WrfTaskManager(config,store,None,Mock()); manager._started=True
            with patch('task_manager.threading.Thread') as thread:
                self.assertFalse(manager._dispatch('two')); thread.assert_not_called()
                store.update('one',runtime={'remote_launch_attempted':True,'remote_terminal_status':'failed'})
                self.assertTrue(manager._dispatch('two')); thread.assert_called_once()

    def test_uncertain_launch_cannot_be_cancelled_locally(self):
        with TemporaryDirectory() as directory:
            config = replace(settings,database_path=Path(directory)/'tasks.sqlite3')
            store=TaskStore(config.database_path); self.addCleanup(store.close)
            store.create('one',{},'owner');store.update('one',status='running',runtime={'remote_launch_attempted':True})
            manager=WrfTaskManager(config,store,None,Mock())
            with patch.object(manager,'_start_remote_cancel') as cancel:
                self.assertEqual(manager.cancel('one')['status'],'cancel_pending');cancel.assert_called_once()

    def test_missing_output_validator_cannot_publish(self):
        with TemporaryDirectory() as directory:
            config=replace(settings,database_path=Path(directory)/'tasks.sqlite3')
            store=TaskStore(config.database_path);self.addCleanup(store.close)
            store.create('one',{},'owner')
            manager=WrfTaskManager(config,store,None,object())
            with self.assertRaisesRegex(RuntimeError,'缺少输出校验器'):
                manager._finalize('one','2026100800')

    def test_process_claim_blocks_replay_pid_reuse_and_parallel_roots(self):
        with TemporaryDirectory() as directory:
            base=Path(directory); first=base/'one'; second=base/'two'
            with patch.dict('os.environ',{'WRF_COMPUTE_RESOURCE_ROOT':directory}), patch.object(process.subprocess,'Popen') as spawn:
                self.assertEqual(process.launch(first,'a'*32,'true')['status'],'unknown')
                process.launch(first,'a'*32,'true');spawn.assert_called_once()
                with self.assertRaisesRegex(RuntimeError,'occupied or unknown'):
                    process.launch(second,'b'*32,'true')
                state=first/'launches'/('a'*32)/'state.json'
                process.save(state,{'status':'running','process':{'pid':42,'start_ticks':'old','boot_id':'boot'}})
                with patch.object(process,'identity',return_value={'pid':42,'start_ticks':'new','boot_id':'boot'}):
                    self.assertEqual(process.inspect(first,'a'*32)['status'],'unknown')

    def test_preparation_only_uploads_task_files(self):
        with TemporaryDirectory() as directory:
            base=Path(directory); config=replace(settings,database_path=base/'db.sqlite3',
                hpc_release_root='/isolated',hpc_workbench_data_root='/data/workbench')
            task_id='wrf_gfs_20261008T000000Z_aaaaaaaa'
            store=TaskStore(config.database_path);self.addCleanup(store.close)
            store.create(task_id,{},'owner')
            store.update(task_id,runtime={'release_id':'pinned','release_fingerprint':'fingerprint'})
            paths=[base/name for name in ('task.json','task.env','forcing.expected.tsv')]
            for path in paths:path.write_text('fixture')
            client=HpcClient(config);client.run=Mock(return_value='');client.upload=Mock()
            with patch('runtime_release.require_ready',return_value={'release_id':'pinned','fingerprint':'fingerprint'}):
                client.prepare_runtime(task_id,*paths,base)
            self.assertEqual([call.args[0].name for call in client.upload.call_args_list],[p.name for p in paths])
            self.assertFalse(any('/backend_wrf_service/' in call.args[1] for call in client.upload.call_args_list))
            self.assertTrue(all(call.args[1].startswith('/data/workbench/tasks/'+task_id+'/control/') for call in client.upload.call_args_list))

    def test_unpinned_task_cannot_write_to_the_legacy_runtime(self):
        client=HpcClient(settings);client.run=Mock();client.upload=Mock()
        with patch.object(client,'_task_runtime',return_value={}):
            with self.assertRaisesRegex(HpcError,'未固定'):
                client.prepare_runtime('wrf_gfs_20261008T000000Z_aaaaaaaa',None,None,None,None)
        client.run.assert_not_called();client.upload.assert_not_called()

    def test_diagnosis_uses_the_persisted_managed_attempt_log(self):
        client = HpcClient(settings)
        client.run = Mock()
        with patch.object(client, '_managed_status', return_value={
                'status': 'failed', 'log': 'x'*30000 + 'attempt failure'}) as status:
            result = client.diagnostic_status('task')
        status.assert_called_once_with('task', timeout=12)
        client.run.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['log']), 24000)
        self.assertTrue(result['log'].endswith('attempt failure'))

    def test_sealed_download_script_is_read_by_bash_without_release_mutation(self):
        config=replace(settings,hpc_runtime_release='pinned',hpc_release_root='/runtime',
                       hpc_gfs_download_script='/legacy/scripts/download_gfs_00z.sh')
        client=HpcClient(config);client.run=Mock(return_value='QUEUED|0,1');client.upload=Mock()
        result=client.trigger_gfs_download('2026100600',forecast_hours=[0,1])
        self.assertEqual(result['status'],'queued')
        command=client.run.call_args.args[0]
        self.assertIn('test -r /runtime/releases/pinned/scripts/download_gfs_00z.sh',command)
        self.assertIn('bash /runtime/releases/pinned/scripts/download_gfs_00z.sh',command)
        self.assertNotIn('/legacy/',command)
        self.assertNotIn('chmod',command)
        client.upload.assert_not_called()

    def test_managed_status_keeps_phase_when_runner_tail_has_lost_marker(self):
        task_id='wrf_gfs_20261008T000000Z_aaaaaaaa'; token='a'*32
        config=replace(settings,hpc_workbench_data_root='/isolated/data')
        client=HpcClient(config)
        client.run=Mock(side_effect=[json.dumps({'status':'running','attempt':token,'log':'WPS inventory'}),
                                     '{"stage":"wps","status":"running","message":"private detail"}\n'])
        with patch.object(client,'_task_runtime',return_value={'launch_attempt':token}), \
                patch.object(client,'service_dir',return_value='/release/scripts'), \
                patch.object(client,'task_dir',return_value='/control'):
            result=client._managed_status(task_id)
        self.assertIn('[stage-status] {"stage": "wps", "status": "running"}',result['log'])
        self.assertNotIn('private detail',result['log'])
        self.assertIn('/attempts/'+token+'/stage_status_'+task_id+'_attempt-1-cpu.jsonl',client.run.call_args.args[0])

    def test_managed_status_does_not_mix_another_attempt_phase(self):
        task_id='wrf_gfs_20261008T000000Z_aaaaaaaa'
        client=HpcClient(settings)
        client.run=Mock(return_value=json.dumps({'status':'unknown','attempt':'b'*32,'log':'old'}))
        with patch.object(client,'_task_runtime',return_value={'launch_attempt':'a'*32}), \
                patch.object(client,'service_dir',return_value='/release/scripts'), \
                patch.object(client,'task_dir',return_value='/control'):
            result=client._managed_status(task_id)
        self.assertEqual(result['log'],'old');self.assertEqual(client.run.call_count,1)
