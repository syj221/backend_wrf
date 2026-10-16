"""Real ASGI routes and auth, isolated stores, no lifespan, SSH or dispatch."""
import base64
import builtins
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import hmac
import importlib.util
import json
import shlex
from pathlib import Path
import sys
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import config
from hpc_transport import HpcClient
from test_ordinary_ui2 import tree_request
from test_recovery_diagnosis import runtime


ROOT = Path(__file__).parent
TASK = "wrf_gfs_20261008T000000Z_aaaaaaaa"
DS = "ds_20261008T000000Z_aaaaaaaa"
SECRET = "synthetic-http-regression-secret"


def headers(owner="alice", role=2, key=None):
    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    encoded = segment({"alg": "HS256"}) + "." + segment({"sub": owner, "role": role, "exp": 4102444800})
    signature = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), encoded.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    value = {"Authorization": "Bearer " + encoded + "." + signature}
    if key is not None:
        value["Idempotency-Key"] = key
    return value


class HttpSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="wrf-http-safety-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        isolated = replace(config.settings, data_dir=self.root / "data", run_dir=self.root / "runs",
                           database_path=self.root / "tasks.sqlite3", downscale_database_path=self.root / "downscale.sqlite3",
                           catalog_data_root=self.root / "catalog",
                           downscale_retired=True, hpc_gfs_prefetch_enabled=False,
                           runtime_asset_root=None,legacy_asset_manifest=None)
        with sqlite3.connect(isolated.downscale_database_path) as db:
            db.execute("CREATE TABLE downscale_tasks(id TEXT PRIMARY KEY,owner_sub TEXT,status TEXT,stage TEXT,progress INTEGER,created_at TEXT,updated_at TEXT,archived_at TEXT)")
        self.scope.enter_context(patch.object(config, "settings", isolated))
        self.scope.enter_context(patch("auth.JWT_SECRET", SECRET))
        self.no_ssh = self.scope.enter_context(patch.object(HpcClient, "run", side_effect=AssertionError("SSH is forbidden in HTTP regression")))
        module_name = "wrf_http_safety_application"
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "main.py")
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.api
        self.addCleanup(sys.modules.pop, module_name, None)
        original_import = builtins.__import__
        def guarded_import(name,*args,**kwargs):
            if name == "recommendations" or name == "downscale" or name.startswith("downscale."):
                raise AssertionError("Retired main must not load legacy execution packages")
            return original_import(name,*args,**kwargs)
        with patch("builtins.__import__",side_effect=guarded_import):
            spec.loader.exec_module(self.api)
        self.addCleanup(self.api.store.close)
        self.assertFalse(hasattr(self.api,"downscale_service"))
        self.assertFalse(hasattr(self.api,"downscale_runner"))
        self.assertFalse(hasattr(self.api,"recommendation_manager"))
        self.scope.enter_context(patch.object(self.api.task_manager, "start", side_effect=AssertionError("Lifespan must not start")))
        self.dispatch = self.scope.enter_context(patch.object(self.api.task_manager, "_dispatch", return_value=False))
        self.scope.enter_context(patch.object(self.api.task_manager, "_execute", side_effect=AssertionError("No real execution")))
        self.ready = self.scope.enter_context(patch.object(self.api, "require_ready", return_value={"status": "ready"}))
        self.namelist_probe = self.api.namelist_runtime
        self.scope.enter_context(patch.object(self.api, "namelist_runtime", return_value=runtime()))
        # No context manager: Starlette only invokes lifespan when entered.
        self.client = TestClient(self.api.app)
        self.addCleanup(self.client.close)
        self.payload = {**tree_request(), "spinup": {"mode": "custom", "hours": 0}, "vertical_levels": 55}

    def test_vertical_conflict_is_rejected_before_submission_or_resource_reservation(self):
        payload = {**self.payload, "vertical_levels": 20}
        for path in ('/api/wrf/tasks/validate', '/api/wrf/tasks'):
            response = self.client.post(path, headers=headers(), json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn('至少设为 33', response.json()['detail'])
        self.dispatch.assert_not_called()
        self.ready.assert_not_called()
        self.assertEqual(self.api.store.list(limit=10), [])

    def test_parameter_preview_reports_conflict_and_pending_without_mutations(self):
        response = self.client.post('/api/wrf/namelist/resolve', headers=headers(),
                                    json={**self.payload, "vertical_levels": 20})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()['data']
        self.assertIn('not enough eta levels', '；'.join(result['errors']))
        self.assertEqual(len([r for r in result['rules'] if r['severity']=='pending']), 3)
        self.assertEqual(result['effective']['input']['domains']['e_vert'], [20]*4)
        self.dispatch.assert_not_called()
        self.ready.assert_not_called()
        self.assertEqual(self.api.store.list(limit=10), [])

    def test_mpi_configuration_error_is_actionable_without_submission(self):
        from runtime_release import MpiConfigurationError
        self.ready.side_effect = MpiConfigurationError("D01 10×10，实际使用 1 进程；请将 nproc_x、nproc_y 设为 -1")
        response = self.client.post('/api/wrf/tasks', headers=headers(), json=self.payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn('实际使用 1', response.json()['detail'])
        self.dispatch.assert_not_called()
        self.assertEqual(self.api.store.list(limit=10), [])

    def tearDown(self):
        self.no_ssh.assert_not_called()
        self.api.task_manager.start.assert_not_called()
        self.assertFalse(hasattr(self.api,"downscale_runner"))

    def test_recommendation_all_entries_retired_without_analysis(self):
        for method, path in (("post", "/api/wrf/recommendations"), ("get", "/api/wrf/recommendations/rec_synthetic")):
            for identity in ({}, headers()):
                with self.subTest(method=method, identity=bool(identity)):
                    response = getattr(self.client, method)(path, headers=identity)
                    self.assertEqual(response.status_code, 410)
        self.assertFalse(hasattr(self.api,"recommendation_manager"))
        self.dispatch.assert_not_called()

    def test_source_probe_uses_the_quoted_isolated_runtime_python(self):
        executable = "/synthetic isolated runtime/bin/python"
        evidence = {"source_hash":"synthetic","binary_hashes":{},"generator_hashes":{}}
        with patch.object(self.api,"settings",replace(self.api.settings,hpc_runtime_python=executable)),patch.object(self.api.hpc_client,"run",return_value=json.dumps(evidence)) as remote:
            self.assertEqual(self.namelist_probe(force=True,timeout=2),evidence)
        self.assertTrue(remote.call_args.args[0].startswith(shlex.quote(executable)+" -B -c "))
        self.assertEqual(remote.call_args.kwargs["timeout"],2)

    def test_retired_downscale_mutations_are_denied_before_payload_or_execution(self):
        for path in ("/downscale/tasks", "/downscale/validate", f"/downscale/tasks/{DS}/retry", f"/downscale/tasks/{DS}/archive"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post("/api/wrf" + path, headers=headers(), content=b"{bad-json").status_code, 410)
        self.assertEqual(self.client.get("/api/wrf/downscale/templates").status_code, 401)
        self.assertEqual(self.client.get("/api/wrf/downscale/templates", headers=headers(role=1)).status_code, 410)
        self.assertFalse(self.api.retired_downscale_query.list_visible("alice",1))

    def test_retired_history_remains_private_and_all_execution_is_gone(self):
        with sqlite3.connect(self.api.settings.downscale_database_path) as db:
            db.execute("INSERT INTO downscale_tasks VALUES(?,?,'cancelled','cancelled',0,'then','now',NULL)",(DS,"alice"))
        path = f"/api/wrf/downscale/tasks/{DS}"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=headers("bob", 1)).status_code, 404)
        self.assertEqual(self.client.get(path, headers=headers("alice", 1)).status_code, 200)
        self.assertEqual(self.client.post(path + "/cancel", headers=headers("bob", 1)).status_code, 410)
        response = self.client.post(path + "/cancel", headers=headers("alice", 1))
        self.assertEqual(response.status_code, 410)
        self.assertFalse(self.client.get("/api/health/activity").json()["data"]["downscale_active"])
        self.dispatch.assert_not_called()

    def test_task_and_unpublished_static_asset_require_owner_or_admin(self):
        self.api.store.create(TASK, self.payload, "alice")
        task_path = f"/api/wrf/tasks/{TASK}"
        asset = self.api.settings.output_dir / "runs" / TASK / "synthetic.webp"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"synthetic-private-frame")
        asset_path = f"/data/WRF/runs/{TASK}/synthetic.webp"
        for path in (task_path, asset_path):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(self.client.get(path, headers=headers("bob", 1)).status_code, 404)
                self.assertEqual(self.client.get(path, headers=headers("alice", 1)).status_code, 200)
                self.assertEqual(self.client.get(path, headers=headers("admin", 3)).status_code, 200)
        self.assertEqual(self.client.get(asset_path, headers=headers("alice", 1)).content, b"synthetic-private-frame")
        self.dispatch.assert_not_called()

    def test_idempotent_submission_preserves_runtime_tree_and_owner_bound_reconciliation(self):
        identity = headers(key="same-key")
        first = self.client.post("/api/wrf/tasks", headers=identity, json=self.payload)
        self.assertEqual(first.status_code, 202, first.text)
        task = first.json()["data"]
        second = self.client.post("/api/wrf/tasks", headers=identity, json=self.payload)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["data"]["id"], task["id"])
        self.assertEqual(len(self.api.store.list(50, "alice")), 1)
        self.assertEqual(task["request"]["spinup"]["hours"], 0)
        self.assertEqual(task["request"]["vertical_levels"], 55)
        self.assertEqual([d["parent_id"] for d in task["request"]["domains"]], [0, 1, 1, 2])
        before = self.dispatch.call_count
        own = self.client.get("/api/wrf/submissions/same-key", headers=headers(role=1))
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json()["data"]["status"], "accepted")
        self.assertEqual(own.json()["data"]["task_id"], task["id"])
        other = self.client.get("/api/wrf/submissions/same-key", headers=headers("bob", 1))
        self.assertEqual(other.json()["data"], {"status": "not_found", "task_id": None})
        self.assertEqual(self.dispatch.call_count, before)
        bob = self.client.post("/api/wrf/tasks", headers=headers("bob", 2, "same-key"), json=self.payload)
        self.assertEqual(bob.status_code, 202, bob.text)
        self.assertNotEqual(bob.json()["data"]["id"], task["id"])
        conflict = self.client.post("/api/wrf/tasks", headers=identity, json={**self.payload, "vertical_levels": 60})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.api.store.get(task["id"])["request"]["vertical_levels"], 55)
        self.assertEqual(len(self.api.store.list(50, "alice")), 1)

    def test_submit_auth_role_and_key_rejections_have_no_side_effects(self):
        self.assertEqual(self.client.post("/api/wrf/tasks", json=self.payload).status_code, 401)
        self.assertEqual(self.client.post("/api/wrf/tasks", headers=headers(role=1), json=self.payload).status_code, 403)
        self.assertEqual(self.client.post("/api/wrf/tasks", headers=headers(key="invalid key"), json=self.payload).status_code, 422)
        self.assertFalse(self.api.store.list())
        self.ready.assert_not_called()
        self.dispatch.assert_not_called()

    def test_blocked_readiness_does_not_reserve_or_queue_a_submission(self):
        self.ready.side_effect = RuntimeError("synthetic blocked release")
        response = self.client.post("/api/wrf/tasks", headers=headers(key="blocked-key"), json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(self.api.store.get_submission("alice", "blocked-key"))
        self.assertFalse(self.api.store.list())
        self.dispatch.assert_not_called()

    def test_confirm_recovery_policy_rejects_legacy_http_execution(self):
        self.api.store.create(TASK, {**self.payload, "recovery_policy": "confirm"}, "alice")
        with ExitStack() as stack:
            execution = [stack.enter_context(patch.object(self.api.task_manager, name, side_effect=AssertionError("Legacy execution forbidden")))
                         for name in ("retry", "restart", "resume_external", "retry_outputs")]
            for suffix in ("retry", "restart", "resume", "retry-outputs"):
                body = {"request": self.payload, "confirm_task_id": TASK, "confirm_attempt": 1} if suffix == "restart" else {}
                with self.subTest(route=suffix):
                    response = self.client.post(f"/api/wrf/tasks/{TASK}/{suffix}", headers=headers(), json=body)
                self.assertEqual(response.status_code, 409, response.text)
            for operation in execution:
                operation.assert_not_called()
        self.dispatch.assert_not_called()

    def test_management_capability_metadata_permissions_and_optimistic_version(self):
        capability=self.client.get('/api/wrf/options',headers=headers(role=1))
        self.assertEqual(capability.status_code,200)
        self.assertEqual(capability.json()['data']['workbench_management']['id'],'wrf-workbench-management-1')
        self.api.store.create(TASK,self.payload,'alice')
        original=self.api.store.get(TASK)
        path=f'/api/wrf/tasks/{TASK}/metadata'
        self.assertEqual(self.client.patch(path,json={'version':0,'name':'private'},headers=headers('bob')).status_code,404)
        self.assertEqual(self.client.patch(path,json={'version':0,'name':'private'},headers=headers(role=1)).status_code,403)
        updated=self.client.patch(path,json={'version':0,'name':'华东试算','notes':'isolated regression'},headers=headers())
        self.assertEqual(updated.status_code,200,updated.text)
        self.assertEqual(self.api.store.get(TASK),original)
        self.assertEqual(self.client.patch(path,json={'version':0,'archived':True},headers=headers()).status_code,409)
        archive=self.client.patch(path,json={'version':1,'archived':True},headers=headers('admin',3))
        self.assertEqual(archive.status_code,200,archive.text)
        self.assertEqual(self.client.get('/api/wrf/tasks',headers=headers()).json()['data']['total'],0)
        listed=self.client.get('/api/wrf/tasks?archived=true&search=华东',headers=headers())
        self.assertEqual(listed.json()['data']['items'][0]['id'],TASK)
        self.assertEqual(self.client.get('/api/wrf/tasks?archived=true',headers=headers('bob')).json()['data']['total'],0)

    def test_legacy_delete_cannot_remove_a_record_or_dispatch(self):
        self.api.store.create(TASK,self.payload,'alice')
        response=self.client.request('DELETE',f'/api/wrf/tasks/{TASK}',json={'confirm_task_id':TASK},headers=headers())
        self.assertEqual(response.status_code,410,response.text)
        self.assertIsNotNone(self.api.store.get(TASK))
        self.dispatch.assert_not_called()

    def test_download_idempotency_ownership_and_reference_release(self):
        payload={'source':'gfs','cycle':'2026100900','start_hour':0,'end_hour':1}
        path='/api/wrf/forcing/downloads'
        self.assertEqual(self.client.post(path,json=payload,headers=headers()).status_code,422)
        with patch.object(self.api.hpc_client,'trigger_forcing_download',return_value={'status':'queued'}) as trigger,\
             patch.object(self.api.hpc_client,'release_forcing_reference',return_value={'status':'released'}) as release:
            first=self.client.post(path,json=payload,headers=headers(key='download-once'))
            self.assertEqual(first.status_code,202,first.text)
            identifier=first.json()['data']['id']
            second=self.client.post(path,json=payload,headers=headers(key='download-once'))
            self.assertEqual(second.json()['data']['id'],identifier)
            trigger.assert_called_once()
            self.assertEqual(self.client.post(path,json={**payload,'end_hour':2},headers=headers(key='download-once')).status_code,409)
            self.assertEqual(self.client.get(path,headers=headers('bob')).json()['data']['items'],[])
            self.assertEqual(self.client.post(f'{path}/{identifier}/cancel',headers=headers('bob')).status_code,404)
            cancelled=self.client.post(f'{path}/{identifier}/cancel',headers=headers())
            self.assertEqual(cancelled.json()['data']['status'],'cancelled')
            release.assert_called_once_with('gfs','2026100900',identifier)

    def test_forcing_cleanup_requires_new_actor_bound_plan(self):
        old={'paths':['/ordinary/2026100100'],'confirm':True}
        for path in ('/api/wrf/forcing/gfs/cleanup','/api/wrf/gfs/cleanup'):
            self.assertEqual(self.client.post(path,json=old,headers=headers('admin',3)).status_code,410)
            self.assertEqual(self.client.post(path,json=old,headers=headers()).status_code,403)
        with patch.object(self.api.forcing_cleanup_service,'plan',return_value={'id':'isolated','digest':'a'*64}) as plan:
            path='/api/wrf/forcing/gfs/cleanup-plans'
            self.assertEqual(self.client.post(path,headers=headers()).status_code,403)
            self.assertEqual(self.client.post(path,headers=headers('admin',3)).status_code,200)
            # An ordinary preview must not release failed-task references.
            plan.assert_called_once_with('gfs','admin',False)
        path='/api/wrf/forcing/cleanup-plans/isolated/execute'
        self.assertEqual(self.client.post(path,json={'digest':'a'*64},headers=headers()).status_code,403)


if __name__ == "__main__":
    unittest.main()
