"""The retained retirement boundary must not import or execute legacy runners."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from downscale_retired import RetiredDownscaleQuery,RetiredStateError,build_retired_router


class RetiredBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="wrf-retired-boundary-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/"retired.sqlite3"
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE downscale_tasks(id TEXT,owner_sub TEXT,status TEXT,stage TEXT,progress INTEGER,created_at TEXT,updated_at TEXT,archived_at TEXT,request_json TEXT,runtime_json TEXT)")
            db.execute("INSERT INTO downscale_tasks VALUES('one','owner','cancelled','cancelled',0,'then','now',NULL,?,?)",(json.dumps({"secret":"private-request"}),json.dumps({"password":"private-runtime"})))
        self.query = RetiredDownscaleQuery(self.path)
        self.app = FastAPI()
        def viewer(request):
            owner = request.headers.get("X-Test-Owner","")
            return owner,int(request.headers.get("X-Test-Role","1"))
        self.app.include_router(build_retired_router(self.query,viewer,lambda request,limit:[{"id":"ordinary"}]))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_queries_preserve_acl_and_never_return_request_runtime_or_owner(self):
        for path in ("/api/wrf/downscale/tasks","/api/wrf/downscale/tasks/one"):
            self.assertEqual(self.client.get(path).status_code,401)
            response = self.client.get(path,headers={"X-Test-Owner":"owner"})
            self.assertEqual(response.status_code,200)
            text = response.text
            for private in ("private-request","private-runtime","owner_sub","request_json","runtime_json"):
                self.assertNotIn(private,text)
        self.assertEqual(self.client.get("/api/wrf/downscale/tasks/one",headers={"X-Test-Owner":"other"}).status_code,404)
        self.assertEqual(self.client.get("/api/wrf/downscale/tasks/one",headers={"X-Test-Owner":"admin","X-Test-Role":"3"}).status_code,200)
        self.assertEqual(self.client.get("/api/wrf/downscale/tasks",headers={"X-Test-Owner":"other"}).json()["data"]["items"],[])

    def test_write_routes_authenticate_then_return_410_without_payload_parsing(self):
        for path in ("tasks","validate","tasks/one/retry","tasks/one/cancel","terrain-assets","anything/new"):
            url = "/api/wrf/downscale/"+path
            self.assertEqual(self.client.post(url,content=b"{bad-json").status_code,401)
            response = self.client.post(url,headers={"X-Test-Owner":"owner","Content-Type":"application/json"},content=b"{bad-json")
            self.assertEqual(response.status_code,410)
            self.assertEqual(response.json()["detail"]["code"],"wrf_downscale_retired")
        for suffix in ("logs","result"):
            self.assertEqual(self.client.get("/api/wrf/downscale/tasks/one/"+suffix,headers={"X-Test-Owner":"other"}).status_code,404)
            self.assertEqual(self.client.get("/api/wrf/downscale/tasks/one/"+suffix,headers={"X-Test-Owner":"owner"}).status_code,410)
        self.assertEqual(self.query.lookup("one")["status"],"cancelled")

    def test_local_activity_is_empty_after_record_removal_and_unknown_fails_closed(self):
        self.assertFalse(self.query.local_activity()["downscale_active"])
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE downscale_tasks SET status='unrecognized'")
        state = self.query.local_activity()
        self.assertTrue(state["downscale_active"])
        self.assertEqual(state["unknown_task_count"],1)
        self.assertNotIn("one",json.dumps(state))
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM downscale_tasks")
        state = self.query.local_activity()
        self.assertEqual(state["task_count"],0)
        self.assertFalse(state["downscale_active"])
        self.assertEqual(self.client.get("/api/wrf/downscale/tasks/one",headers={"X-Test-Owner":"owner"}).status_code,404)
        self.assertEqual(self.client.get("/api/wrf/workbench/tasks",headers={"X-Test-Owner":"owner"}).json()["data"]["items"],[{"id":"ordinary"}])

    def test_readonly_queries_do_not_create_or_migrate_files(self):
        before = self.path.read_bytes()
        self.query.lookup("one")
        self.query.list_visible("owner",1)
        self.query.local_activity()
        self.assertEqual(before,self.path.read_bytes())
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),["retired.sqlite3"])
        absent = self.path.with_name("absent.sqlite3")
        missing = RetiredDownscaleQuery(absent)
        self.assertTrue(missing.local_activity()["downscale_active"])
        with self.assertRaises(RetiredStateError):
            missing.lookup("one")
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main()
