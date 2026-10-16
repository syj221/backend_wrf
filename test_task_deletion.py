"""Compute-free deletion gates for owner-bound plans and immutable manifests."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from database import TaskStore
from task_deletion import TaskDeletionService, remaining_valid, inventory
from retirement import scan_root, remote_session_absent


class TaskDeletionTest(unittest.TestCase):
    def test_execute_and_reconcile_after_uncertain_remote_reply(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);task_id="wrf_gfs_20261009T000000Z_01234567"
            store=TaskStore(root/"tasks.sqlite3");self.addCleanup(store.close)
            store.create(task_id,{"data_source":"gfs"},"alice")
            store.update(task_id,status="succeeded",stage="done")
            settings=SimpleNamespace(run_dir=root/"runs",output_dir=root/"output",
                catalog_data_root=root/"catalog",hpc_workbench_data_root="",hpc_remote_dir="/remote")
            owned=settings.run_dir/task_id;owned.mkdir(parents=True);(owned/"result.txt").write_text("owned")
            client=SimpleNamespace(task_dir=lambda _:"/remote/control/"+task_id,
                remote_output_dir=lambda _:"/remote/WRF_"+task_id+"/run",
                task_artifact_paths=lambda _:["/remote/artifact/"+task_id])
            catalog={"task_id":task_id,"file_uuid":"fixture","record":None,"assets":[],"references":[]}
            uncertain=[True]
            def remote(_client,op,payload,timeout=3600):
                if op=="paths":return payload
                if op=="discover":return []
                if op=="terminal":return [{"verified":True,"status":"succeeded"}]
                if op=="session_absent":return [{"verified":True}]
                if op=="scan":return [{"path":p,"exists":False,"entries":[],"file_count":0,
                    "symlink_count":0,"bytes":0} for p in payload]
                if op=="delete":
                    if uncertain[0]:
                        uncertain[0]=False
                        raise RuntimeError("remote reply lost")
                    return []
                raise AssertionError(op)
            def catalog_call(_engine,_ids,revoke=False,purge=False,expected=None):
                return [catalog.copy()]
            with patch("task_deletion.remote_call",side_effect=remote),\
                 patch("task_deletion.init_database",return_value=(object(),None)),\
                 patch("task_deletion.catalog_snapshot",side_effect=catalog_call),\
                 patch("task_deletion.observe_static_access",return_value=[]):
                service=TaskDeletionService(settings,store,client)
                plan=service.plan(task_id,"alice",directory_mode=False)
                with self.assertRaisesRegex(RuntimeError,"remote reply lost"):
                    service.execute(task_id,plan["id"],"alice",plan["digest"],"Bearer isolated-fixture")
                self.assertEqual(service.status(task_id,plan["id"],"alice")["status"],"unknown")
                self.assertTrue(owned.exists())
                result=service.reconcile(task_id,plan["id"],"alice","Bearer isolated-fixture")
                self.assertEqual(result["status"],"complete")
                self.assertFalse(owned.exists())
                self.assertIsNone(store.get(task_id))

    def test_preview_claim_and_tombstone_preserve_submission(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            task_id = "wrf_gfs_20261009T000000Z_01234567"
            store = TaskStore(root / "tasks.sqlite3")
            self.addCleanup(store.close)
            store.create(task_id,{"data_source":"gfs"},"alice")
            store.update(task_id,status="succeeded",stage="done")
            settings=SimpleNamespace(run_dir=root/"runs",output_dir=root/"output",catalog_data_root=root/"catalog",hpc_workbench_data_root="",hpc_remote_dir="/remote")
            client=SimpleNamespace(task_dir=lambda _:"/remote/control/"+task_id,
                remote_output_dir=lambda _:"/remote/WRF_"+task_id+"/run",
                task_artifact_paths=lambda _:["/remote/artifact/"+task_id])
            def remote(_client,op,payload,timeout=3600):
                if op=="paths":return payload
                if op=="discover":return []
                if op=="terminal":return [{"verified":True,"status":"succeeded"}]
                if op=="scan":return [{"path":p,"exists":False,"entries":[],"file_count":0,"symlink_count":0,"bytes":0} for p in payload]
                raise AssertionError(op)
            catalog=[{"task_id":task_id,"file_uuid":"fixture","record":None,"assets":[],"references":[]}]
            with patch("task_deletion.remote_call",side_effect=remote),patch("task_deletion.init_database",return_value=(object(),None)),patch("task_deletion.catalog_snapshot",return_value=catalog):
                service=TaskDeletionService(settings,store,client)
                plan=service.plan(task_id,"alice",directory_mode=False)
                self.assertEqual(plan["summary"]["files"],0)
                with self.assertRaisesRegex(ValueError,"不属于"):
                    service.status(task_id,plan["id"],"bob")
                self.assertTrue(store.claim_deletion(plan["id"],task_id))
                self.assertTrue(store.deletion_blocked(task_id))
                self.assertFalse(store.claim_deletion(plan["id"],task_id))
                store.purge_deleted_task(task_id,plan["id"])
                self.assertIsNone(store.get(task_id))
                self.assertEqual(store.deletion(plan["id"])["status"],"complete")

    def test_partial_manifest_reconciliation_accepts_only_deletions(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)/"owned";root.mkdir();(root/"a").write_text("a");(root/"b").write_text("b")
            original=scan_root(root)
            (root/"a").unlink()
            self.assertTrue(remaining_valid(original,scan_root(root)))
            (root/"b").write_text("tampered")
            self.assertFalse(remaining_valid(original,scan_root(root)))

    def test_live_task_cannot_be_previewed(self):
        with TemporaryDirectory() as directory:
            store=TaskStore(Path(directory)/"tasks.sqlite3")
            self.addCleanup(store.close)
            task_id="wrf_gfs_20261009T000000Z_01234567"
            store.create(task_id,{"data_source":"gfs"},"alice")
            with self.assertRaisesRegex(ValueError,"仍可能运行"):
                TaskDeletionService(SimpleNamespace(),store,Mock()).plan(task_id,"alice")

    def test_partial_result_uses_successful_compute_evidence_and_unlaunched_cancel_needs_absent_roots(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);identity='wrf_gfs_20261009T000000Z_01234567'
            store=TaskStore(root/'wrf.sqlite3');self.addCleanup(store.close)
            store.create(identity,{'data_source':'gfs'},'alice')
            cfg=SimpleNamespace(run_dir=root/'runs',output_dir=root/'out',catalog_data_root=root/'catalog',hpc_workbench_data_root='',hpc_remote_dir='/remote')
            client=SimpleNamespace(task_dir=lambda _:'/remote/control/'+identity,remote_output_dir=lambda _:'/remote/WRF_'+identity+'/run',task_artifact_paths=lambda _:[])
            requests=[]
            def remote(_client,operation,payload,**kwargs):
                if operation=='paths':return payload
                if operation=='discover':return []
                if operation=='scan':return [{'path':path,'exists':False,'entries':[],'bytes':0,'file_count':0,'symlink_count':0} for path in payload]
                if operation=='terminal':
                    requests.extend(payload)
                    return [{'verified':True,'status':payload[0]['status']}]
                raise AssertionError(operation)
            catalog=[{'task_id':identity,'references':[],'assets':[]}]
            with patch('task_deletion.remote_call',side_effect=remote),patch('task_deletion.init_database',return_value=(None,None)),patch('task_deletion.catalog_snapshot',return_value=catalog):
                store.update(identity,status='partial_success',stage='rendering')
                self.assertEqual(inventory(cfg,store,client,identity)['terminal']['status'],'succeeded')
                self.assertEqual(requests[0]['status'],'succeeded')
                store.update(identity,status='cancelled',stage='cancelled')
                self.assertTrue(inventory(cfg,store,client,identity)['terminal']['never_started'])
                self.assertEqual(len(requests),1)

    def test_waiting_restart_preview_uses_strict_never_launched_proof(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);identity='wrf_gfs_20261009T000000Z_01234567'
            store=TaskStore(root/'wrf.sqlite3');self.addCleanup(store.close)
            store.create(identity,{'data_source':'gfs'},'alice')
            store.update(identity,status='waiting_restart',stage='failed',runtime={'release_id':'ordinary-03'},failure={'stage':'checking_hpc_forcing'})
            cfg=SimpleNamespace(run_dir=root/'runs',output_dir=root/'out',catalog_data_root=root/'catalog',hpc_workbench_data_root='/remote/workbench',hpc_remote_dir='/remote',hpc_runtime_python='/fixed/python')
            client=SimpleNamespace(task_dir=lambda _:'/remote/workbench/tasks/'+identity+'/control',remote_output_dir=lambda _:'/remote/workbench/tasks/'+identity+'/wrf/run',run=Mock(return_value='{"mode":"not_launched","terminal":true,"control_absent":true,"attempts_absent":true}'))
            def remote(_client,op,payload,**kwargs):
                if op=='paths':return payload
                if op=='scan':return [{'path':p,'exists':False,'entries':[],'file_count':0,'symlink_count':0,'bytes':0} for p in payload]
                raise AssertionError('Unexpected legacy probe '+op)
            with patch('task_deletion.remote_call',side_effect=remote),patch('task_deletion.init_database',return_value=(None,None)),patch('task_deletion.catalog_snapshot',return_value=[{'task_id':identity,'references':[],'assets':[]}]):
                proof=inventory(cfg,store,client,identity)['terminal']
                self.assertTrue(proof['never_started'])
                self.assertEqual(proof['mode'],'not_launched')
                client.run.assert_called_once()
                store.update(identity,runtime={'release_id':'ordinary-03','remote_pid':123})
                with self.assertRaisesRegex(RuntimeError,'cannot be proven'):
                    inventory(cfg,store,client,identity)
                self.assertEqual(client.run.call_count,1)


    def test_reconciliation_fails_closed_without_retained_process_identity(self):
        with self.assertRaisesRegex(RuntimeError,'identity'):
            remote_session_absent({'verified':True,'status':'succeeded'})
        self.assertEqual(remote_session_absent({'verified':True,'never_started':True}),{'verified':True})


if __name__=="__main__":unittest.main()


class RuntimeIdentityReadTest(unittest.TestCase):
    def test_database_read_failure_does_not_infer_legacy_paths(self):
        from hpc_transport import HpcClient, HpcError
        with TemporaryDirectory() as directory:
            path=Path(directory)/"broken.sqlite3"
            path.write_bytes(b"not a sqlite database")
            client=object.__new__(HpcClient)
            client.settings=SimpleNamespace(database_path=path,hpc_remote_dir="/legacy")
            with self.assertRaisesRegex(HpcError,"identity cannot be read"):
                client.task_dir("wrf_gfs_20261009T000000Z_01234567")
