"""Reference protection is independent of download activity or model liveness."""
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock

from config import settings
from task_manager import WrfTaskManager


class ForcingReferenceProtectionTest(unittest.TestCase):
    def test_pool_hours_are_observed_not_assumed_to_start_at_zero(self):
        from hpc_transport import HpcClient
        for source, hours in [('gfs',[3,4]),('ecmwf',[3,6])]:
            client=HpcClient(settings)
            client._pool_queue_state=Mock(return_value={})
            client.run=Mock(return_value='ROOT|/pool\nHOURS|2026100900|'+','.join('%03d'%h for h in hours)+'\n2026100900|partial|2|100|'+str(hours[-1])+'|0|0|0|')
            cycle=client.forcing_pool_items(source,[],set())[0]['cycles'][0]
            self.assertEqual(cycle['available_hours'],hours)
            self.assertEqual(cycle['forecast_start'],3)
            self.assertEqual(cycle['forecast_end'],hours[-1])

    def test_unbound_queued_window_protects_all_eligible_cycles_and_counts_once(self):
        from test_namelist_config import request
        payload = {**request(), 'configuration_profile':'wrf-ordinary-ui-2',
                   'start_time':'2026-10-09T03:00:00Z', 'end_time':'2026-10-09T06:00:00Z',
                   'spinup':{'hours':6}, 'forecast_interval_hours':1}
        queued = {'id':'queued-fixture','request':payload,'runtime':{}}
        store = Mock();store.active.return_value=[queued]
        store.with_status.return_value=[];store.forcing_download_cycles.return_value=set()
        manager = WrfTaskManager(settings,store,None,Mock())
        counts = manager.task_forcing_reference_counts('gfs')
        self.assertEqual(counts,{'2026100800':1,'2026100700':1})
        self.assertEqual(manager.active_forcing_cycles('gfs'),set(counts))
        queued['request']={}
        with self.assertRaisesRegex(ValueError,'禁止清理'):
            manager.active_forcing_cycles('gfs')

    def test_pending_recovery_keeps_each_source_cycle_protected(self):
        def task(source, cycle):
            return {"request": {"data_source": source}, "runtime": {"forcing_cycle": cycle}}
        store = Mock()
        store.forcing_download_cycles.return_value = set()
        store.active.return_value = [task("gfs", "2026100900")]
        states = {
            "paused_external": [task("ecmwf", "2026100800")],
            "waiting_restart": [task("gfs", "2026100600")],
            "failed": [task("ecmwf", "2026100600")],
            "partial_success": [task("gfs", "2026100700")],
        }
        states["cancelled"] = []
        store.with_status.side_effect = lambda state: states[state]
        manager = WrfTaskManager(settings, store, None, Mock())
        self.assertEqual(manager.active_forcing_cycles("gfs"), {"2026100600", "2026100700", "2026100900"})
        self.assertEqual(manager.active_forcing_cycles("ecmwf"), {"2026100600", "2026100800"})

    def test_download_sync_has_no_implicit_deletion(self):
        store, gfs, hpc = Mock(), Mock(), Mock()
        store.forcing_download_cycles.return_value = set()
        store.active.return_value = []
        store.with_status.return_value = []
        gfs.latest_cycles.return_value = ["2026100900"]
        pool = [{"cycles": [{"cycle": "2026100900", "complete": False}]}]
        hpc.gfs_pool_items.return_value = pool
        hpc.forcing_pool_items.return_value = pool
        hpc.trigger_gfs_download.return_value = {"cycle": "2026100900", "status": "queued"}
        hpc.trigger_forcing_download.return_value = {"cycle": "2026100900", "status": "queued"}
        manager = WrfTaskManager(settings, store, gfs, hpc)
        self.assertEqual(manager.sync_latest_gfs()["actions"][0]["status"], "queued")
        self.assertEqual(manager.sync_latest_ecmwf()["actions"][0]["status"], "queued")
        hpc.auto_cleanup_gfs_cycles.assert_not_called()
        hpc.auto_cleanup_forcing_cycles.assert_not_called()

    def test_explicit_gfs_download_route_does_not_delete_data(self):
        tree = ast.parse(Path(__file__).with_name("main.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "trigger_remote_gfs")
        function.decorator_list = []
        hpc = Mock()
        hpc.trigger_gfs_download.return_value = {"status": "queued"}
        env = {"Any": Any, "RemoteGfsTriggerRequest": object, "hpc_client": hpc,
               "ok": lambda data, **kw: data, "HpcError": RuntimeError}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "<isolated-download-route>", "exec"), env)
        result = env["trigger_remote_gfs"](SimpleNamespace(cycle="2026100900"))
        self.assertEqual(result["status"], "queued")
        hpc.trigger_gfs_download.assert_called_once_with("2026100900", 72)
        hpc.auto_cleanup_gfs_cycles.assert_not_called()


if __name__ == '__main__':
    unittest.main()
