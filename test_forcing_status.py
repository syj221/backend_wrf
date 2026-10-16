"""Presentation states must reflect publication and transfer evidence."""
from unittest import TestCase
from unittest.mock import patch
from config import Settings
from hpc_transport import HpcClient


class ForcingStatusTest(TestCase):
    def details(self, publication='published', status='queued'):
        return HpcClient._queue_cycle_details({'cycles':{'2026101400':{
            'publication':publication,'active_file':'stale-file',
            'publication_error':'HTTP 404 Bearer private-path',
            'files':{'000':{'status':status,'current_file':'f000','download_rate_bps':23}}}}},'2026101400')

    def test_publication_wait_overrides_queued_files_and_redacts_reason(self):
        for state in ('waiting_publish','probing'):
            value=self.details(state)
            self.assertEqual(value['queue_status'],'waiting_publish')
            self.assertIn('f072',value['wait_reason'])
            self.assertNotIn('Bearer',value['wait_reason'])
            self.assertIsNone(value['current_file'])
            self.assertEqual(value['download_rate_bps'],0)

    def test_queue_without_transfer_is_not_downloading(self):
        value=self.details()
        self.assertEqual(value['queue_status'],'queued')
        self.assertIn('旧版工作器',value['wait_reason'])
        self.assertIsNone(value['current_file'])

    def test_provider_wait_reasons_are_specific_and_expire(self):
        import time
        state = {'transport_policy': 'per-provider-1', 'cycles': {'2026101600': {
            'publication': 'published', 'files': {'000': {'status': 'queued'}},
            'wait_code': 'cycle_busy', 'wait_observed_at': int(time.time())}}}
        self.assertIn('周期锁', HpcClient._queue_cycle_details(state, '2026101600')['wait_reason'])
        state['cycles']['2026101600']['wait_observed_at'] = 0
        self.assertEqual(HpcClient._queue_cycle_details(state, '2026101600')['wait_reason'], '等待本源下载工作器调度')
        state['cycles']['2026101500'] = {'files': {'003': {'status': 'downloading', 'current_file': 'f003'}}}
        self.assertIn('2026101500 · f003', HpcClient._queue_cycle_details(state, '2026101600')['wait_reason'])

    def test_real_transfer_wins_over_stale_publication_metadata(self):
        value=self.details('waiting_publish','downloading')
        self.assertEqual(value['queue_status'],'downloading')
        self.assertEqual(value['current_file'],'f000')
        self.assertEqual(value['download_rate_bps'],23)
        self.assertEqual(value['wait_reason'],'')

    def test_both_providers_keep_ready_and_queued_protection(self):
        for source,total in (('gfs',73),('ecmwf',25)):
            client=HpcClient(Settings())
            for file_status,publication,expected in [('queued','waiting_publish','waiting_publish'),('queued','published','queued'),('downloading','published','downloading'),('ready','published','ready'),('queued','waiting_publish','ready')]:
                count=total if expected=='ready' else 0
                state={'cycles':{'2026101400':{'publication':publication,'files':{'000':{'status':file_status}}}}}
                raw=f'ROOT|/pool\n2026101400|'+('ready' if count else 'partial')+f'|{count}|0|0|0|0|0|'
                with patch.object(client,'_pool_queue_state',return_value=state),patch.object(client,'run',return_value=raw):
                    pool=client.forcing_pool_items(source,['2026101400'],set())[0]
                self.assertEqual(pool['cycles'][0]['status'],expected,(source,file_status,pool))
                self.assertEqual(pool['status'],expected)
                self.assertFalse(pool['cycles'][0]['cleanup_allowed'])

    def test_non_target_queue_keeps_original_active_cleanup_protection(self):
        for source in ('gfs','ecmwf'):
            client=HpcClient(Settings())
            state={'cycles':{'2026101400':{'publication':'published','files':{'000':{'status':'queued'}}}}}
            with patch.object(client,'_pool_queue_state',return_value=state),patch.object(client,'run',return_value='ROOT|/pool\n2026101400|partial|0|0|0|0|0|0|'):
                item=client.forcing_pool_items(source,['2026101500'],set())[0]['cycles'][0]
            self.assertEqual(item['status'],'queued')
            self.assertFalse(item['target'])
            self.assertFalse(item['cleanup_allowed'])


class PrefetchSummaryTest(TestCase):
    def manager(self, gfs_status, ec_status):
        from unittest.mock import Mock
        from task_manager import WrfTaskManager
        store,gfs,hpc=Mock(),Mock(),Mock()
        store.forcing_policy.return_value={'enabled':True,'auto_cleanup':False,'held_cycles':{'gfs':[],'ecmwf':[]}}
        store.active.return_value=[];store.with_status.return_value=[]
        store.forcing_download_cycles.return_value=set()
        gfs.latest_cycles.return_value=['2026101400']
        def pool(status,total):
            return [{'cycles':[{'cycle':'2026101400','status':status,'complete':status=='ready',
                'completed_files':total if status=='ready' else 18,'total_files':total,
                'current_file':'f018' if status=='downloading' else None,
                'partial_size_bytes':4096 if status=='downloading' else 0,
                'download_rate_bps':1234 if status=='downloading' else 0,
                'wait_reason':'等待发布' if status=='waiting_publish' else ''}]}]
        hpc.gfs_pool_items.return_value=pool(gfs_status,73)
        hpc.forcing_pool_items.return_value=pool(ec_status,25)
        hpc.trigger_gfs_download.return_value={'cycle':'2026101400','status':'queued','detail':'ack'}
        hpc.trigger_forcing_download.return_value={'cycle':'2026101400','status':'queued','detail':'ack'}
        manager=WrfTaskManager(Settings(),store,gfs,hpc)
        manager._health={'status':'ready'};manager._check_health=Mock()
        return manager

    def test_two_sources_share_observed_status_and_keep_enqueue_count(self):
        cases=[('downloading','waiting_publish','downloading'),('waiting_publish','downloading','downloading'),
               ('queued','queued','queued'),('waiting_publish','waiting_publish','waiting_publish'),('ready','ready','ready')]
        for gfs_status,ec_status,expected in cases:
            with self.subTest(gfs=gfs_status,ecmwf=ec_status):
                manager=self.manager(gfs_status,ec_status)
                manager._prefetch_latest_gfs_once()
                summary=manager.gfs_prefetch_status
                self.assertEqual(summary['status'],expected)
                self.assertEqual([v['status'] for v in summary['actions']],[gfs_status,ec_status])
                for action in summary['actions']:
                    if action['status']=='downloading':
                        self.assertEqual(action['current_file'],'f018')
                        self.assertIn('下载中',action['detail'])
                        self.assertEqual(action['completed_files'],18)
                self.assertEqual(manager.hpc.trigger_gfs_download.call_count,int(gfs_status!='ready'))
                self.assertEqual(manager.hpc.trigger_forcing_download.call_count,int(ec_status!='ready'))
                manager.hpc.auto_cleanup_gfs_cycles.assert_not_called()
                manager.hpc.auto_cleanup_forcing_cycles.assert_not_called()

    def test_completed_during_enqueue_is_ready_not_stale_queued(self):
        manager=self.manager('ready','ready')
        incomplete=[{'cycles':[{'cycle':'2026101400','complete':False}]}]
        manager.hpc.gfs_pool_items.side_effect=[incomplete,manager.hpc.gfs_pool_items.return_value]
        manager.hpc.forcing_pool_items.side_effect=[incomplete,manager.hpc.forcing_pool_items.return_value]
        manager._prefetch_latest_gfs_once()
        self.assertEqual(manager.gfs_prefetch_status['status'],'ready')
        self.assertEqual([v['detail'] for v in manager.gfs_prefetch_status['actions']],['73/73','25/25'])
        manager.hpc.trigger_gfs_download.assert_called_once()
        manager.hpc.trigger_forcing_download.assert_called_once()

    def test_missing_observation_preserves_old_service_acknowledgement(self):
        from task_manager import apply_forcing_observations
        actions=[{'cycle':'2026101400','status':'queued','detail':'ack'}]
        apply_forcing_observations(actions,{'cycles':[]})
        self.assertEqual(actions,[{'cycle':'2026101400','status':'queued','detail':'ack'}])
