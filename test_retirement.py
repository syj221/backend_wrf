"""Retirement safety gates use synthetic files and databases only."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import uuid

from sqlalchemy import create_engine,text

from retirement import (SCHEMA,apply_manifest,catalog_snapshot,database_records,delete_root,
                        exclusive_tasks,remote_call,scan_root,sha,_apply_locked,manifest_digest,
                        discover_task_paths,dedicated_environment_roots,make_manifest)
from retirement import shared_forcing_references,environment_occupancy_blocked
from retirement import legacy_retirement_assets,retirement_databases,purge_legacy_mapping,reused_remote_inventory

DS = "ds_20260918T120000Z_15711b1a"
WRF = "wrf_gfs_20260918T120000Z_4501020c"


class RetirementTest(unittest.TestCase):
    def test_directory_replacement_between_stat_and_open_is_not_unsealed(self):
        with TemporaryDirectory(prefix="wrf-retirement-inode-race-") as temporary:
            root = Path(temporary)
            target, replacement, original = (root/name for name in ("approved", "replacement", "original"))
            target.mkdir(); replacement.mkdir()
            (target/"owned").write_text("approved")
            (replacement/"protected").write_text("preserve")
            target.chmod(0o555); replacement.chmod(0o555)
            snapshot = scan_root(str(target))
            real_open = os.open
            replaced = False
            def open_after_substitution(path, flags, *args, **kwargs):
                nonlocal replaced
                if path == target.name and kwargs.get("dir_fd") is not None and not replaced:
                    replaced = True
                    target.rename(original); replacement.rename(target)
                return real_open(path, flags, *args, **kwargs)
            try:
                with patch("retirement.os.open",side_effect=open_after_substitution),patch("retirement.os.fchmod") as chmod:
                    with self.assertRaisesRegex(RuntimeError,"Directory identity changed"):
                        delete_root(snapshot,verify=False,make_writable=True)
                    chmod.assert_not_called()
                self.assertTrue(replaced)
                self.assertEqual((target/"protected").read_text(),"preserve")
                self.assertEqual((original/"owned").read_text(),"approved")
                self.assertEqual(target.stat().st_mode & 0o777,0o555)
            finally:
                for directory in (target,replacement,original):
                    if directory.exists():directory.chmod(0o755)

    def test_exact_readonly_staging_cleanup_unseals_only_bound_directories(self):
        with TemporaryDirectory(prefix="wrf-retirement-sealed-") as temporary:
            root = Path(temporary)
            authoritative = root/"shared-input"
            authoritative.write_text("preserve")
            target = root/"failed-staging"
            child = target/"sealed"
            child.mkdir(parents=True)
            (child/"runtime-file").write_text("copy")
            (child/"shared-link").symlink_to(authoritative)
            child.chmod(0o555);target.chmod(0o555)
            try:
                snapshot = scan_root(str(target))
                delete_root(snapshot,make_writable=True)
                self.assertFalse(target.exists())
                self.assertEqual(authoritative.read_text(),"preserve")
            finally:
                for directory in (target,child):
                    if directory.exists():directory.chmod(0o755)

    def test_static_verification_does_not_forward_bearer_on_redirect(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from threading import Thread
        from retirement import observe_static_access
        requests = {"source":0,"destination":0}
        class Destination(BaseHTTPRequestHandler):
            def do_HEAD(self):
                requests["destination"] += 1
                self.send_response(200);self.end_headers()
            def log_message(self,*args):pass
        destination = ThreadingHTTPServer(("127.0.0.1",0),Destination)
        class Redirect(BaseHTTPRequestHandler):
            def do_HEAD(self):
                requests["source"] += 1
                self.send_response(302)
                self.send_header("Location",f"http://127.0.0.1:{destination.server_port}/unexpected")
                self.end_headers()
            def log_message(self,*args):pass
        source = ThreadingHTTPServer(("127.0.0.1",0),Redirect)
        threads = [Thread(target=server.serve_forever,daemon=True) for server in (source,destination)]
        for thread in threads:thread.start()
        try:
            catalog = [{"task_id":DS,"record":True,"assets":[]}]
            with patch("retirement.DISPLAY_STATIC_ORIGIN",f"http://127.0.0.1:{source.server_port}"):
                result = observe_static_access(catalog,"synthetic-test-token")
            self.assertEqual(result[0]["status"],302)
            self.assertTrue(result[0]["authenticated"])
            self.assertEqual(requests,{"source":1,"destination":0})
        finally:
            for server in (source,destination):server.shutdown();server.server_close()
            for thread in threads:thread.join(timeout=2)
            self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_migrated_source_and_active_rows_are_both_bound_without_other_task_loss(self):
        with TemporaryDirectory(prefix="wrf-retirement-migration-") as temporary:
            root = Path(temporary)
            paths = {name:root/(name+".sqlite3") for name in ("old","active","downscale")}
            request = {"highres_parent_task_id":DS}
            for name,path in paths.items():
                table = "downscale_tasks" if name == "downscale" else "wrf_tasks"
                with sqlite3.connect(path) as db:
                    db.execute("CREATE TABLE "+table+"(id TEXT,owner_sub TEXT,created_at TEXT,status TEXT,request_json TEXT,runtime_json TEXT)")
                    identity = DS if name == "downscale" else WRF
                    db.execute("INSERT INTO "+table+" VALUES(?,?,?,?,?,?)",(identity,"owner","then","cancelled",json.dumps(request),"{}"))
                    db.execute("INSERT INTO "+table+" VALUES('other','other','then','succeeded','{}','{}')")
            mapping = root/"legacy-assets.json"
            value = {"schema":"wrf-local-assets-1","target_database":str(paths["active"]),
                     "database_mappings":[{"kind":"ordinary","source":str(paths["old"]),"target":str(paths["active"])}],
                     "legacy_roots":{"run_dir":str(root/"old-runs"),"output_dir":str(root/"old-outputs")},
                     "tasks":{WRF:{"identity_digest":"selected"},"other":{"identity_digest":"keep"}}}
            mapping.write_text(json.dumps(value))
            settings = SimpleNamespace(database_path=paths["active"],downscale_database_path=paths["downscale"],legacy_asset_manifest=mapping)
            legacy = legacy_retirement_assets(settings)
            ordinary = {"id":WRF,"owner_sub":"owner","created_at":"then","request":request}
            bindings = retirement_databases(settings,[{"id":DS},ordinary],[ordinary],legacy)
            self.assertEqual(len(bindings),3)
            self.assertEqual(sum(row["count"] for item in bindings for row in item["records"]),3)
            for item in bindings:
                database_records(Path(item["path"]),set(item["task_ids"]),item["kind"],delete=True,expected=item["records"])
                self.assertFalse(any(row["count"] for row in database_records(Path(item["path"]),set(item["task_ids"]),item["kind"])))
            for name,path in paths.items():
                with sqlite3.connect(path) as db:
                    table = "downscale_tasks" if name == "downscale" else "wrf_tasks"
                    self.assertEqual(db.execute("SELECT id FROM "+table).fetchall(),[("other",)])
            purge_legacy_mapping({**legacy,"task_ids_to_remove":[WRF]})
            self.assertEqual(json.loads(mapping.read_text())["tasks"],{"other":{"identity_digest":"keep"}})
            value["database_mappings"][0]["source"] = str(paths["active"])
            mapping.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,"isolated"):
                legacy_retirement_assets(settings)

    def test_legacy_database_copy_cannot_delete_shared_or_changed_upstream(self):
        settings = SimpleNamespace(database_path=Path("/active.sqlite3"),downscale_database_path=Path("/ds.sqlite3"))
        ordinary = {"id":WRF,"owner_sub":"owner","created_at":"then","request":{"highres_parent_task_id":DS}}
        legacy = {"database_mappings":[{"kind":"ordinary","source":"/old.sqlite3","target":"/active.sqlite3"}]}
        variants = [{**ordinary,"status":"succeeded","runtime":{},"owner_sub":"changed"},
                    {**ordinary,"status":"running","runtime":{}}]
        for old in variants:
            with patch("retirement.read_tasks",return_value=[old]),self.assertRaisesRegex(ValueError,"ownership"):
                retirement_databases(settings,[ordinary],[ordinary],legacy)
        old = {**ordinary,"status":"succeeded","runtime":{}}
        with patch("retirement.read_tasks",return_value=[old,{"id":"other","runtime":{"source":WRF}}]),self.assertRaisesRegex(ValueError,"shared"):
            retirement_databases(settings,[ordinary],[ordinary],legacy)

    def test_remote_hash_reuse_requires_exact_complete_metadata_match(self):
        with TemporaryDirectory(prefix="wrf-retirement-cache-") as temporary:
            path = Path(temporary)/"output"
            path.write_text("initial")
            snapshot = scan_root(str(path))
            previous = {"schema":SCHEMA,"remote":[snapshot]}
            previous["digest"] = manifest_digest(previous)
            def remote(client,operation,roots,**kwargs):
                self.assertEqual(operation,"metadata")
                metadata = scan_root(str(path),hash_files=False)
                return [{"path":metadata["path"],"metadata_sha256":sha(metadata)}]
            with patch("retirement.remote_call",side_effect=remote):
                self.assertEqual(reused_remote_inventory(Mock(),[str(path)],previous,60),[snapshot])
                path.write_text("changed")
                with self.assertRaisesRegex(RuntimeError,"metadata changed"):
                    reused_remote_inventory(Mock(),[str(path)],previous,60)

    def test_opaque_system_exception_binds_pid_generation_boot_and_argv0(self):
        identity = {"pid":100,"start_ticks":500,"boot_id":"56365d4c-453f-478a-9a77-49332b34707d",
                    "argv0_sha256":"a"*64,"comm":"sshd"}
        ownership = {"protected_process_exceptions":[identity]}
        occupancy = {"matches":[],"unreadable_owned_processes":[{**identity,"unreadable_surfaces":["exe","cwd","fd","maps"]}]}
        self.assertFalse(environment_occupancy_blocked(occupancy,ownership))
        for key,value in (("pid",101),("start_ticks",501),("boot_id","another-boot"),("argv0_sha256","b"*64),("comm","wrf.exe")):
            changed = deepcopy(occupancy)
            changed["unreadable_owned_processes"][0][key] = value
            self.assertTrue(environment_occupancy_blocked(changed,ownership))
        self.assertTrue(environment_occupancy_blocked({**occupancy,"matches":[{"pid":100}]},ownership))
        self.assertTrue(environment_occupancy_blocked(occupancy,{}))
        self.assertTrue(environment_occupancy_blocked({},ownership))

    def test_attempt_discovery_covers_unsuffixed_and_arbitrary_attempts_only(self):
        with TemporaryDirectory(prefix="wrf-retirement-discovery-") as temporary:
            root = Path(temporary)
            expected = ["WPS_"+WRF,"logs_"+WRF,"WRF_"+WRF+"_attempt-13-cpu",
                        "stage_status_"+WRF+"_attempt-5-gpu.jsonl"]
            for name in expected+["WPS_"+WRF+"_unrelated","WRF_GPU_DATA","WPS_"+WRF+"9"]:
                (root/name).touch()
            self.assertEqual(discover_task_paths(str(root),[WRF]),sorted(str(root/name) for name in expected))
            with self.assertRaises(ValueError):
                discover_task_paths(str(root),["../WRF_GPU"])

    def test_environment_ownership_cannot_extend_to_shared_toolchain(self):
        settings = SimpleNamespace(hpc_downscale_openfoam_activate="/managed/CFD/activate.sh",
                                   hpc_downscale_interface_root="/managed/CFD/interface",
                                   hpc_downscale_cfd_template="/managed/CFD/templates/thermal-rans-graded-v2",
                                   hpc_downscale_python="/managed/ENV/wrfpost/bin/python")
        ownership = {"schema":"wrf-downscale-environment-ownership-1","dedicated_roots":["/managed/ENV/wrfpost"],
                     "classification":[{"path":"/managed/ENV/wrfpost","dedicated":True}],
                     "evidence":{"roots":[{"path":"/managed/ENV/wrfpost"}],"processes":[],"lsof":{"matches":[],"returncode":0},"scan_errors":[]}}
        self.assertEqual(dedicated_environment_roots(settings,ownership),["/managed/ENV/wrfpost"])
        for change in ({"dedicated_roots":["/shared/anaconda3"]},{"evidence":{"processes":[{"pid":10}]}},
                       {"evidence":{"scan_errors":["permission_denied"]}}):
            with self.assertRaises(ValueError):
                dedicated_environment_roots(settings,{**ownership,**change})

    def test_manifest_includes_declared_and_discovered_historical_paths(self):
        with TemporaryDirectory(prefix="wrf-retirement-manifest-") as temporary:
            root = Path(temporary)
            settings = SimpleNamespace(downscale_database_path=root/"ds.sqlite3",database_path=root/"wrf.sqlite3",
                                       run_dir=root/"run",output_dir=root/"output",catalog_data_root=root/"catalog",
                                       downscale_retired=True,hpc_remote_dir="/managed/RUNTIME",hpc_storage_alias="/managed")
            ds = {"id":DS,"runtime":{"source_wrf":{"task_id":WRF,"auto_created":True}},"request":{},"status":"cancelled","updated_at":"now"}
            ordinary = {"id":WRF,"runtime":{},"request":{"highres_parent_task_id":DS},"status":"succeeded","updated_at":"then"}
            client = Mock()
            client.downscale_task_dir.return_value="/managed/control/"+DS
            client.downscale_output_dir.return_value="/managed/DOWNSCALE_"+DS+"/run"
            client.task_dir.return_value="/managed/control/"+WRF
            client.remote_output_dir.return_value="/managed/RUNTIME/WRF_"+WRF+"/run"
            client.task_artifact_paths.return_value=["/managed/RUNTIME/WPS_"+WRF+"_attempt-2-cpu"]
            scanned=[]
            def remote(client,operation,payload,**kwargs):
                if operation == "discover":return ["/managed/RUNTIME/logs_"+WRF]
                if operation == "scan":scanned.extend(payload)
                return []
            with patch("retirement.read_tasks",side_effect=[[ds],[ordinary]]),patch("retirement.remote_call",side_effect=remote),patch("retirement.catalog_snapshot",return_value=[]),patch("retirement.database_records",return_value=[]),patch("retirement.observe_static_access",return_value=[]),patch("retirement.shared_forcing_references",return_value=[]):
                make_manifest(settings,client,Mock(),[DS])
            self.assertIn("/managed/RUNTIME/logs_"+WRF,scanned)
            self.assertIn("/managed/RUNTIME/WPS_"+WRF+"_attempt-2-cpu",scanned)
            self.assertIn("/managed/WRFwork/DOWNSCALE_"+DS,scanned)
            self.assertNotIn("/managed/WRFwork/DOWNSCALE_"+DS+"/run",scanned)

    def test_retained_task_forcing_reference_blocks_exclusive_source_deletion(self):
        with TemporaryDirectory(prefix="wrf-retirement-forcing-") as temporary:
            path = Path(temporary)/"forcing.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE mesoscale_forcing_artifacts(artifact_id TEXT,manifest_json TEXT)")
                db.execute("INSERT INTO mesoscale_forcing_artifacts VALUES(?,?)",("forcing-source",json.dumps({"source_task_id":WRF})))
            retained = {"id":"ds_20260919T120000Z_15711b1a","request":{"upstream_artifact_id":"forcing-source"}}
            self.assertEqual(shared_forcing_references(path,{DS,WRF},[retained]),[{"artifact_id":"forcing-source","referencing_tasks":[retained['id']]}])
            self.assertEqual(shared_forcing_references(path,{DS,WRF,retained['id']},[retained]),[])

    def test_inventory_hashes_regular_files_without_following_symlinks(self):
        with TemporaryDirectory(prefix="wrf-retirement-test-") as temporary:
            root = Path(temporary)/"dedicated"
            root.mkdir()
            authoritative = Path(temporary)/"authoritative"
            authoritative.write_bytes(b"shared authority")
            (root/"input.copy").write_bytes(b"exclusive")
            (root/"shared.link").symlink_to(authoritative)
            snapshot = scan_root(str(root))
            self.assertEqual(snapshot["file_count"],1)
            self.assertEqual(snapshot["symlink_count"],1)
            self.assertEqual(snapshot["bytes"],9)
            self.assertEqual(next(e["sha256"] for e in snapshot["entries"] if e["kind"] == "file"),hashlib.sha256(b"exclusive").hexdigest())
            delete_root(snapshot)
            self.assertFalse(root.exists())
            self.assertEqual(authoritative.read_bytes(),b"shared authority")

    def test_symlink_root_is_unlinked_and_parent_substitution_is_rejected(self):
        with TemporaryDirectory(prefix="wrf-retirement-test-") as temporary:
            root = Path(temporary)
            authority = root/"authority"
            authority.mkdir()
            (authority/"input").write_text("keep")
            link = root/"link"
            link.symlink_to(authority,target_is_directory=True)
            snapshot = scan_root(str(link))
            self.assertEqual(snapshot["file_count"],0)
            delete_root(snapshot)
            self.assertEqual((authority/"input").read_text(),"keep")
            target = root/"task"
            target.mkdir()
            (target/"output").write_text("before")
            snapshot = scan_root(str(target))
            (target/"output").write_text("changed")
            with self.assertRaisesRegex(RuntimeError,"changed"):
                delete_root(snapshot)
            self.assertTrue(target.exists())

    def test_exclusive_upstream_requires_both_markers_and_no_other_reference(self):
        ds = {"id":DS,"runtime":{"source_wrf":{"task_id":WRF,"auto_created":True}}}
        ordinary = {"id":WRF,"request":{"highres_parent_task_id":DS}}
        self.assertEqual(exclusive_tasks([ds],[ordinary],[DS])[1],[ordinary])
        for change in (False,None):
            item = deepcopy(ds)
            item["runtime"]["source_wrf"]["auto_created"] = change
            self.assertEqual(exclusive_tasks([item],[ordinary],[DS])[1],[])
        peer = {"id":"ds_20260919T120000Z_15711b1a","runtime":{"source_wrf":{"task_id":WRF}}}
        self.assertEqual(exclusive_tasks([ds,peer],[ordinary],[DS])[1],[])
        with self.assertRaises(ValueError):
            exclusive_tasks([ds],[ordinary],["../data"])

    def test_exact_record_fingerprint_and_transactional_family_removal(self):
        with TemporaryDirectory(prefix="wrf-retirement-db-") as temporary:
            path = Path(temporary)/"tasks.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE wrf_tasks(id TEXT,request_json TEXT,owner_sub TEXT);CREATE TABLE wrf_incidents(task_id TEXT,data_json TEXT);CREATE TABLE wrf_recovery_plans(id TEXT,data_json TEXT)")
                db.executemany("INSERT INTO wrf_tasks VALUES(?,?,?)",[(WRF,"{}","private-owner"),("unrelated","{}","other")])
                db.executemany("INSERT INTO wrf_incidents VALUES(?,?)",[(WRF,"{}"),("unrelated","{}")])
                db.execute("INSERT INTO wrf_recovery_plans VALUES(?,?)",("plan",json.dumps({"task_id":WRF})))
            expected = database_records(path,{WRF},"ordinary")
            self.assertNotIn("private-owner",json.dumps(expected))
            changed = deepcopy(expected)
            changed[0]["sha256"]="invalid"
            with self.assertRaises(RuntimeError):
                database_records(path,{WRF},"ordinary",delete=True,expected=changed)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM wrf_tasks").fetchone()[0],2)
            database_records(path,{WRF},"ordinary",delete=True,expected=expected)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT id FROM wrf_tasks").fetchall(),[("unrelated",)])
                self.assertEqual(db.execute("SELECT task_id FROM wrf_incidents").fetchall(),[("unrelated",)])
                self.assertEqual(db.execute("SELECT count(*) FROM wrf_recovery_plans").fetchone()[0],0)

    def catalog_fixture(self,temporary):
        engine = create_engine("sqlite:///"+str(Path(temporary)/"catalog.sqlite3"))
        with engine.begin() as db:
            db.execute(text("CREATE TABLE public_info(file_uuid TEXT PRIMARY KEY,data_type TEXT,source_path TEXT,meta_path TEXT,is_deleted INTEGER,file_hash TEXT,source_file_uuid TEXT,delete_time TEXT,delete_reason TEXT)"))
            db.execute(text("CREATE TABLE wrf_info(asset_uuid TEXT,file_uuid TEXT,dataset_id TEXT,parsed_data_path TEXT,webp_url TEXT,asset_status TEXT)"))
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL,"wrf-workbench:"+WRF))
            db.execute(text("INSERT INTO public_info VALUES(:id,'WRF',:source,:meta,0,'hash',NULL,NULL,NULL)"),{"id":identity,"source":"workbench/"+WRF+"/scene.meta.json","meta":"WRF/workbench/"+WRF+"/scene.meta.json"})
            db.execute(text("INSERT INTO wrf_info VALUES('asset',:id,:task,:path,:url,'ready')"),{"id":identity,"task":WRF,"path":"WRF/workbench/"+WRF+"/frame.webp","url":"/data/WRF/workbench/"+WRF+"/frame.webp"})
        self.addCleanup(engine.dispose)
        return engine,identity

    def test_catalog_revoke_then_purge_preserves_unrelated_rows(self):
        with TemporaryDirectory(prefix="wrf-retirement-catalog-") as temporary:
            engine,identity = self.catalog_fixture(temporary)
            approved = catalog_snapshot(engine,[WRF])
            catalog_snapshot(engine,[WRF],revoke=True,expected=approved)
            with engine.connect() as db:
                self.assertEqual(db.execute(text("SELECT is_deleted FROM public_info")).scalar(),1)
                self.assertEqual(db.execute(text("SELECT asset_status FROM wrf_info")).scalar(),"retired")
            catalog_snapshot(engine,[WRF],purge=True,expected=approved)
            with engine.connect() as db:
                self.assertEqual(db.execute(text("SELECT count(*) FROM public_info")).scalar(),0)
                self.assertEqual(db.execute(text("SELECT count(*) FROM wrf_info")).scalar(),0)

    def test_catalog_reference_or_identity_mismatch_rolls_back(self):
        with TemporaryDirectory(prefix="wrf-retirement-catalog-") as temporary:
            engine,identity = self.catalog_fixture(temporary)
            approved = catalog_snapshot(engine,[WRF])
            with engine.begin() as db:
                db.execute(text("INSERT INTO public_info(file_uuid,source_file_uuid) VALUES('shared',:id)"),{"id":identity})
            with self.assertRaises(RuntimeError):
                catalog_snapshot(engine,[WRF],revoke=True,expected=approved)
            with engine.connect() as db:
                self.assertEqual(db.execute(text("SELECT is_deleted FROM public_info WHERE file_uuid=:id"),{"id":identity}).scalar(),0)
            with engine.begin() as db:
                db.execute(text("UPDATE public_info SET meta_path='WRF/other/scene.meta.json' WHERE file_uuid=:id"),{"id":identity})
            with self.assertRaisesRegex(RuntimeError,"identity"):
                catalog_snapshot(engine,[WRF])

    def test_apply_requires_exact_confirmation_and_refuses_blocked_manifest(self):
        manifest = {"schema":SCHEMA,"selected_task_ids":[DS],"blockers":["remote_inventory_missing"]}
        manifest["digest"] = sha(manifest)
        client = Mock()
        with self.assertRaises(ValueError):
            apply_manifest(manifest,"not-approved",None,client,None,Path("unused"))
        with self.assertRaisesRegex(RuntimeError,"blockers"):
            apply_manifest(manifest,manifest["digest"],None,client,None,Path("unused"))
        client.run.assert_not_called()

    def test_apply_refuses_active_ordinary_task_before_any_mutation(self):
        with TemporaryDirectory(prefix="wrf-retirement-active-") as temporary:
            root = Path(temporary)
            database = root/"ordinary.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE wrf_tasks(id TEXT,status TEXT,runtime_json TEXT,failure_json TEXT)")
                db.execute("INSERT INTO wrf_tasks VALUES('active','running','{}',NULL)")
            settings = SimpleNamespace(port=0,database_path=database)
            with patch("retirement.make_manifest") as inventory,self.assertRaisesRegex(RuntimeError,"must be terminal"):
                _apply_locked({},"approved",settings,Mock(),Mock(),root/"audit.json",timeout=1)
            inventory.assert_not_called()
            self.assertFalse((root/"audit.json").exists())

    def test_apply_preserves_waiting_workflow_and_demands_fresh_remote_terminal_proof(self):
        with TemporaryDirectory(prefix="wrf-retirement-waiting-") as temporary:
            root = Path(temporary)
            database = root/"ordinary.sqlite3"
            runtime = {"remote_launch_attempted":True,"remote_pid":123,
                       "remote_terminal_status":"failed","remote_observation":{"status":"failed","exit_code":1}}
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE wrf_tasks(id TEXT,status TEXT,runtime_json TEXT,failure_json TEXT)")
                db.execute("INSERT INTO wrf_tasks VALUES(?,?,?,NULL)",(WRF,"waiting_restart",json.dumps(runtime)))
            settings = SimpleNamespace(port=0,database_path=database,hpc_runtime_python="python3")
            client = Mock()
            client.task_dir.return_value = "/synthetic/"+WRF
            client.run.return_value = json.dumps({"mode":"legacy_failed","terminal":True,"pid":123,"exit_code":1})
            manifest = {"selected_task_ids":[DS]}
            with patch("retirement.make_manifest",side_effect=RuntimeError("inspection reached")) as inventory:
                with self.assertRaisesRegex(RuntimeError,"inspection reached"):
                    _apply_locked(manifest,"approved",settings,client,Mock(),root/"audit.json",timeout=1)
                inventory.assert_called_once()
            client.run.assert_called_once()
            self.assertIn("Waiting-restart task still has live",client.run.call_args.args[0])
            client.run.reset_mock()
            client.run.return_value = json.dumps({"mode":"legacy_failed","terminal":False})
            with patch("retirement.make_manifest") as inventory,self.assertRaisesRegex(RuntimeError,"invalid proof"):
                _apply_locked(manifest,"approved",settings,client,Mock(),root/"audit.json",timeout=1)
            inventory.assert_not_called()
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute("SELECT status,runtime_json FROM wrf_tasks").fetchone(),
                                 ("waiting_restart",json.dumps(runtime)))
            self.assertFalse((root/"audit.json").exists())

    def test_embedded_remote_inventory_is_executable_and_readonly(self):
        import subprocess
        import shlex
        class LocalClient:
            def run(self,command,timeout):
                parts = shlex.split(command)
                return subprocess.check_output([sys.executable,"-B",*parts[2:]],text=True,timeout=timeout)
        with TemporaryDirectory(prefix="wrf-retirement-remote-") as temporary:
            path = Path(temporary)/"data"
            path.write_bytes(b"synthetic")
            local = scan_root(str(path))
            self.assertEqual(remote_call(LocalClient(),"scan",[str(path)]),[local])
            self.assertTrue(path.exists())
            remote_call(LocalClient(),"delete",[{"path":str(path),"snapshot_sha256":sha(local)}])
            self.assertFalse(path.exists())

    def test_apply_phases_remove_only_approved_records_and_require_static_denial(self):
        for denied in (403,401):
            with self.subTest(status=denied),TemporaryDirectory(prefix="wrf-retirement-apply-") as temporary:
                root = Path(temporary)
                engine,_ = self.catalog_fixture(temporary)
                databases = {}
                for kind,table,identity in (("ordinary","wrf_tasks",WRF),("downscale","downscale_tasks",DS)):
                    path = root/(kind+".sqlite3")
                    with sqlite3.connect(path) as db:
                        db.execute("CREATE TABLE "+table+"(id TEXT,request_json TEXT,owner_sub TEXT,status TEXT,runtime_json TEXT,failure_json TEXT)")
                        db.execute("INSERT INTO "+table+" VALUES(?,?,?,?,?,?)",(identity,"{}","owner","cancelled","{}",None))
                    databases[kind] = path
                assets = root/"exclusive"
                assets.mkdir()
                (assets/"output").write_text("exclusive")
                manifest = {"schema":SCHEMA,"selected_task_ids":[DS],"exclusive_upstream_task_ids":[WRF],
                            "tasks":[{"task_id":DS},{"task_id":WRF}],"terminal_evidence":[],"summary":{"file_count":1},
                            "local":[scan_root(str(assets))],"remote":[],"catalog":catalog_snapshot(engine,[DS,WRF]),
                            "records":{kind:database_records(path,{WRF if kind == 'ordinary' else DS},kind) for kind,path in databases.items()},"blockers":[]}
                manifest["digest"] = manifest_digest(manifest)
                settings = SimpleNamespace(port=0,database_path=databases["ordinary"],downscale_database_path=databases["downscale"])
                audit = root/"audit.json"
                with patch("retirement.make_manifest",return_value=manifest),patch("retirement.remote_call",return_value=[]),patch("retirement.observe_static_access",side_effect=[[{"status":200}],[{"status":denied}]]),patch.dict(os.environ,{"WRF_RETIREMENT_BEARER":"synthetic"}):
                    if denied == 403:
                        result = _apply_locked(manifest,manifest["digest"],settings,Mock(),engine,audit,timeout=1)
                        self.assertEqual(result["phase"],"complete")
                        self.assertFalse(assets.exists())
                        self.assertFalse(catalog_snapshot(engine,[WRF])[0]["record"])
                        self.assertEqual(audit.stat().st_mode & 0o777,0o600)
                    else:
                        with self.assertRaisesRegex(RuntimeError,"files retained"):
                            _apply_locked(manifest,manifest["digest"],settings,Mock(),engine,audit,timeout=1)
                        self.assertTrue(assets.exists())
                        self.assertEqual(json.loads(audit.read_text())["phase"],"catalog_revoked")
                        self.assertTrue(any(r["count"] for r in database_records(databases["ordinary"],{WRF},"ordinary")))


if __name__ == "__main__":
    unittest.main()
