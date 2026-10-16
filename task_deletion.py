"""Manifest-bound ordinary task deletion. No shared forcing or runtime roots."""
from __future__ import annotations

import copy
import json
import re
import time
import threading
import logging
from types import MethodType
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from catalog_publisher import init_database
from retirement import catalog_snapshot, delete_root, observe_static_access, remote_call, scan_root, sha
from runtime_assets import legacy_entry
from hpc import HpcError, HpcAuthError
from terminal_evidence import verify_waiting_restart, terminal_mode
from deletion_queue import DeletionQueue, timings
import directory_delete as directories

TASK_ID = re.compile(r"^wrf_(?:gfs|ecmwf|ec)_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}$")
TERMINAL = {"succeeded", "partial_success", "failed", "cancelled", "waiting_restart"}


def _roots(settings, client, task):
    identity = task["id"]
    if not TASK_ID.fullmatch(identity):
        raise ValueError("任务编号不符合普通 WRF 受管格式")
    local = [settings.run_dir / identity, settings.output_dir / "runs" / identity,
             settings.catalog_data_root / "WRF" / "workbench" / identity,
             settings.catalog_data_root / "WRF" / "workbench" / ("."+identity+".publishing")]
    entry = legacy_entry(settings, task)
    if entry:
        local += [Path(entry["roots"]["run_dir"]) / identity,
                  Path(entry["roots"]["output_dir"]) / "runs" / identity]
    control, output = client.task_dir(identity), client.remote_output_dir(identity)
    remote = [control, str(Path(output).parent) if Path(output).name == "run" else output]
    if task.get("runtime", {}).get("release_id") and settings.hpc_workbench_data_root:
        root = Path(settings.hpc_workbench_data_root) / "tasks" / identity
        if Path(control).is_relative_to(root):
            remote = [str(root)]
    else:
        remote += client.task_artifact_paths(identity)
        remote += remote_call(client,"discover",{"root":settings.hpc_remote_dir,"identities":[identity]})
    resolved = remote_call(client,"paths",remote+[control])
    if len(resolved) != len(remote)+1 or any(not isinstance(path,str) or not Path(path).is_absolute() for path in resolved):
        raise RuntimeError("远端受管路径规范化响应无效")
    remote,control = resolved[:-1],resolved[-1]
    unique = sorted(set(remote), key=lambda value: (len(value), value))
    remote = [path for path in unique if not any(path.startswith(parent.rstrip("/")+"/") for parent in unique if parent != path)]
    return sorted(set(map(str, local))), remote, control


def inventory(settings, store, client, task_id, progress=lambda _phase: None, directory_mode=False):
    progress("checking_state")
    task = store.get(task_id)
    if not task:
        raise ValueError("任务不存在")
    if task["status"] not in TERMINAL:
        raise ValueError("任务仍可能运行；请先取消并核验进程退出")
    local_roots, remote_roots, control = _roots(settings, client, task)
    runtime = task.get("runtime") or {}
    progress("inventory_assets")
    remote = directories.remote(client, "identity", remote_roots) if directory_mode else remote_call(client, "scan", remote_roots)
    if sorted(item["path"] for item in remote) != sorted(remote_roots):
        raise RuntimeError("远端路径清单不完整")
    expected = runtime.get("remote_terminal_status")
    if expected not in {"succeeded", "failed", "cancelled"}:
        expected = "succeeded" if task["status"] == "partial_success" else "failed" if task["status"] == "waiting_restart" else task["status"]
    if task["status"] == "waiting_restart" or (task["status"]=="cancelled" and runtime.get("launch_attempt") and runtime.get("remote_process") is None):
        proof = verify_waiting_restart(settings, client, task)
        terminal = [{**proof, "verified": True, "status": expected,
                     "never_started": proof.get("never_started") is True or proof["mode"] == "not_launched",
                     "process": runtime.get("remote_process") or {},
                     "legacy_pid": runtime.get("remote_pid")}]
    elif task["status"] in {"cancelled","failed"} and not any(runtime.get(key) for key in ("launch_attempt","remote_pid","remote_launch_attempted","remote_wrf_succeeded")) and all(not item["exists"] for item in remote):
        terminal = [{"verified":True,"never_started":True,"status":expected}]
    else:
        terminal = remote_call(client, "terminal", [{"control": control,
            "attempt": runtime.get("launch_attempt"), "status": expected}], timeout=60)
    if len(terminal) != 1 or not terminal[0].get("verified"):
        raise RuntimeError("远端计算会话终态无法核验")
    progress("inventory_assets")
    local = [directories.identity(path) if directory_mode else (scan_root(path,deadline=client.deadline) if isinstance(client,_DeadlineClient) else scan_root(path)) for path in local_roots]
    if directory_mode:
        # Config aliases may resolve to the same root; move each physical root once.
        unique = {item["path"]:item for item in local}
        local = [item for path,item in unique.items() if not any(path.startswith(parent.rstrip('/')+'/') for parent in unique if parent!=path)]
    progress("checking_references")
    engine, _ = init_database(import_users=True)
    catalog = catalog_snapshot(engine, [task_id])
    if catalog[0]["references"]:
        raise RuntimeError("正式产品仍被其他记录引用")
    # A retained task may refer to this task even without a catalog relation.
    from retirement import references
    with store._lock:
        rows = store._db.execute("SELECT id,request_json,runtime_json FROM wrf_tasks WHERE id!=?", (task_id,)).fetchall()
    if any(references(json.loads(row["request_json"]), task_id) or
           references(json.loads(row["runtime_json"]), task_id) for row in rows):
        raise RuntimeError("该任务仍被其他模拟引用")
    result = {"schema": "wrf-task-directory-deletion-2" if directory_mode else "wrf-task-deletion-1", "task_id": task_id,
              "task_fingerprint": sha([task["status"], task["stage"], task["attempt_no"], task["request"], task["runtime"], task.get("result")]),
              "terminal": terminal[0], "terminal_request": {"control":control,
                  "attempt":runtime.get("launch_attempt"),"status":expected},
              "local": local, "remote": remote, "catalog": catalog}
    result["summary"] = {"files": sum(item["file_count"] for item in local+remote),
                         "bytes": sum(item["bytes"] for item in local+remote),
                         "symlinks": sum(item["symlink_count"] for item in local+remote),
                         "published_assets": len(catalog[0]["assets"]), "directory_mode":directory_mode,
                         "protected": ["共享 GFS/ECMWF 资料池", "其他任务", "WRF 发布环境与源码"]}
    result["digest"] = sha(result)
    return result


def maintenance_unmoved_proof(settings, plan, task, tombstone, client):
    """Narrow read-only reload exception; never releases a tombstone or deletes."""
    manifest = plan.get("manifest",{})
    if (not task or plan.get("status") != "unknown" or (plan.get("phase") != "quarantining" and not plan.get("reconciliation_requested"))
            or plan.get("removed") or manifest.get("schema") != "wrf-task-directory-deletion-2"
            or plan.get("task_id") != task["id"] or task["status"] not in TERMINAL
            or not tombstone or tombstone.get("deletion_plan_id") != plan["id"] or tombstone.get("status") != "deleting"):
        raise RuntimeError("Unmoved deletion proof rejected")
    fingerprint = sha([task["status"],task["stage"],task["attempt_no"],task["request"],task["runtime"],task.get("result")])
    if fingerprint != manifest.get("task_fingerprint"):
        raise RuntimeError("Unmoved deletion task changed")
    moves = {kind:[directories.intent(item,plan["id"]) for item in manifest[kind]] for kind in ("local","remote")}
    if moves != plan.get("quarantine"):
        raise RuntimeError("Unmoved deletion intent changed")
    local_roots,remote_roots,_ = _roots(settings,client,task)
    observed_local = [directories.identity(path) for path in local_roots]
    unique = {item["path"]:item for item in observed_local}
    observed_local = [item for path,item in unique.items() if not any(path.startswith(parent.rstrip('/')+'/') for parent in unique if parent!=path)]
    if observed_local != manifest["local"] or directories.remote(client,"identity",remote_roots) != manifest["remote"]:
        raise RuntimeError("Unmoved deletion roots changed")
    locations = directories.remote(client,"location",moves["remote"])+[directories.location(item) for item in moves["local"]]
    if len(locations) != len(moves["remote"])+len(moves["local"]) or any(item not in {"unmoved","absent"} for item in locations):
        raise RuntimeError("Unmoved deletion has moved or missing roots")
    evidence = remote_call(client,"terminal",[manifest["terminal_request"]],timeout=30)
    if len(evidence) != 1 or evidence[0].get("verified") is not True:
        raise RuntimeError("Unmoved deletion terminal identity is uncertain")
    return {"kind":"unmoved_deletion","operation_id":plan["id"],"task":task["id"],
            "fingerprint":fingerprint,"locations":locations,"terminal":evidence[0]}


def remaining_valid(original, current):
    """A partial deletion may only remove original entries, never replace them."""
    if not current["exists"]:
        return True
    if current["path"] != original["path"] or not original["exists"]:
        return False
    expected = {entry["relative"]:entry for entry in original["entries"]}
    for entry in current["entries"]:
        before = expected.get(entry["relative"])
        if not before or entry["kind"] != before["kind"]:
            return False
        if entry["kind"] == "directory":
            if (entry["device"],entry["inode"]) != (before["device"],before["inode"]):
                return False
        elif entry != before:
            return False
    return True


class TaskDeletionService:
    def __init__(self, settings, store, client, operation_lock=None):
        self.settings, self.store, self.client = settings, store, client
        self._operation_lock = operation_lock or (lambda _task_id: nullcontext())
        self.queue=DeletionQueue()
        self._worker=self.queue.worker
        self._clients={}
        self._preview_lock = threading.Lock()

    def close(self):
        self.queue.close()

    def _enqueue(self, identifier, callback):
        def run():
            try:
                callback()
            except Exception as exc:
                logging.getLogger(__name__).exception("WRF deletion operation %s failed", identifier)
                plan = self.store.deletion(identifier)
                if plan:
                    status = "unknown" if self.store.deletion_blocked(plan["task_id"]) else "blocked"
                    message = "核验未完成，未执行删除；请检查计算会话或远端连接。"
                    detail = str(exc).lower()
                    if isinstance(exc, TimeoutError) or "timeout" in detail or "timed out" in detail or "超时" in detail:
                        message = "远端核验超时，核验未完成；未继续删除。"
                    elif "conflicts with launch evidence" in detail:
                        message="原尝试存在冲突的启动记录，暂不能删除。"
                    elif "identity" in detail or "身份" in detail or "persisted" in detail or "managed launch" in detail:
                        message = "缺少可核验的远端进程身份，未继续删除；请由管理员核对原尝试记录。"
                    elif "running" in detail or "仍可能运行" in detail:
                        message = "计算会话尚未确认退出；请先取消并核验全部进程退出。"
                    elif "引用" in detail or "reference" in detail:
                        message = "专属资产仍有其他引用，未继续删除。"
                    elif isinstance(exc, ValueError):
                        message = "任务状态、清单或操作条件已变化；请重新预览，未继续删除。"
                    if status=="unknown":message="删除结果待确认，已保留原操作，请勿重复提交。"
                    self.store.update_deletion_detail(identifier, status=status,
                        **({"phase":"quarantining"} if plan.get("reconciliation_requested") and not plan.get("removed") else {}),
                        error={"code":"WRF_DELETION_UNVERIFIED", "message":message,
                               "operation_id":identifier,"category":deletion_error_category(exc)}, finished_at=time.time())
            finally:
                self._clients.pop(identifier,None)
        def started(now):
            self._clients[identifier]=_DeadlineClient(self.client)
            self.store.update_deletion_detail(identifier,started_at=now,phase="checking_state",expires_at=now+1800)
        self.queue.submit(identifier,run,started)

    def plan_async(self, task_id, actor, *, delete_now=False, bearer=None):
        if delete_now and not bearer:
            raise ValueError("缺少删除凭据")
        task = self.store.get(task_id)
        if not task or task["status"] not in TERMINAL:
            raise ValueError("任务仍可能运行；请先取消并核验进程退出")
        revision = sha([task["status"],task["stage"],task["attempt_no"],task["request"],task["runtime"],task.get("result")])
        with self._preview_lock:
            with self.store._lock:
                rows = self.store._db.execute("SELECT id FROM wrf_task_deletion_plans WHERE task_id=? AND actor_sub=? AND status IN ('checking','pending','executing','unknown')", (task_id,actor)).fetchall()
            for row in rows:
                before = self.store.deletion(row["id"])
                if before["status"] in {"executing","unknown"} and self.store.deletion_blocked(task_id):
                    return self._public(before)
                if before.get("revision") == revision and (before["status"]=="checking" or before["expires_at"] > time.time()):
                    if delete_now and before["status"] == "pending":
                        return self.execute_async(task_id,before["id"],actor,before["manifest"]["digest"],bearer)
                    # A preview already running must finish before it can be
                    # promoted. Do not create a second transaction.
                    if delete_now and before["status"] == "checking" and not before.get("delete_now"):
                        raise ValueError("任务已有未完成操作，请稍后删除")
                    return self._public(before)
            plan = {"id":"delete_"+uuid4().hex,"task_id":task_id,"actor_sub":actor,
                    "created_at":time.time(),"expires_at":time.time()+600,
                    "status":"checking","phase":"queued","revision":revision,
                    "delete_now":delete_now,"task_label":(task.get("metadata") or {}).get("name") or str(task["request"].get("start_time") or task_id)[:64]}
            self.store.save_deletion(plan)
            def prepare():
                if (task.get("runtime") or {}).get("launch_attempt") and not (task.get("runtime") or {}).get("remote_wrf_succeeded"):
                    terminal_mode(task)
                def phase(value):
                    self.store.update_deletion_detail(plan["id"], phase=value)
                snapshot = inventory(self.settings,self.store,self._clients[plan["id"]],task_id,phase,directory_mode=True)
                if snapshot["task_fingerprint"] != revision:
                    raise ValueError("任务状态已变化，请重新预览")
                self.store.update_deletion_detail(plan["id"],manifest=snapshot,status="pending",
                    phase="preview",expires_at=time.time()+600)
                if delete_now and self.store.reserve_deletion_execution(plan["id"]):
                    with self._operation_lock(task_id):
                        self.execute(task_id,plan["id"],actor,snapshot["digest"],bearer,_reserved=True)
            try:
                self._enqueue(plan["id"],prepare)
            except Exception:
                self.store.update_deletion_detail(plan["id"],status="blocked",phase="queue_full")
                raise
        return self.status(task_id,plan["id"],actor)

    def execute_async(self, task_id, plan_id, actor, digest, bearer):
        plan = self.store.deletion(plan_id)
        if not plan or plan["task_id"] != task_id or plan["actor_sub"] != actor:
            raise ValueError("删除计划不存在或不属于当前操作员")
        if plan.get("execution_requested"):
            return self._public(plan)
        if plan["status"] != "pending" or plan["expires_at"] < time.time() or digest != plan["manifest"]["digest"] or not bearer:
            raise ValueError("删除计划已失效或缺少确认凭据")
        if not self.store.reserve_deletion_execution(plan_id):
            return self.status(task_id,plan_id,actor)
        def execute_reserved():
            with self._operation_lock(task_id):
                self.execute(task_id,plan_id,actor,digest,bearer,_reserved=True)
        try:
            self._enqueue(plan_id,execute_reserved)
        except Exception:
            self.store.update_deletion_detail(plan_id,execution_requested=False,phase="preview")
            raise
        return self.status(task_id,plan_id,actor)

    def plan(self, task_id, actor, *, directory_mode=True):
        snapshot = inventory(self.settings, self.store, self.client, task_id, directory_mode=directory_mode)
        plan = {"id": "delete_"+uuid4().hex, "task_id": task_id, "actor_sub": actor,
                "created_at": time.time(), "expires_at": time.time()+600,
                "status": "pending", "phase": "preview", "manifest": snapshot}
        self.store.save_deletion(plan)
        return self._public(plan)

    def status(self, task_id, plan_id, actor):
        plan = self.store.deletion(plan_id)
        if not plan or plan["task_id"] != task_id or plan["actor_sub"] != actor:
            raise ValueError("删除计划不存在或不属于当前操作员")
        return self._public(plan)

    def _public(self,plan):
        basic = {key: plan[key] for key in ("id", "task_id", "created_at", "expires_at", "status", "phase")}
        basic.update(timings(plan,self.queue.position(plan["id"])))
        basic.update(elapsed_seconds=round(max(0,plan.get("finished_at",time.time())-plan["created_at"]),1),
                     execution_requested=bool(plan.get("execution_requested")),error=plan.get("error"),
                     delete_now=bool(plan.get("delete_now")), removed=bool(plan.get("removed")),
                     removed_at=plan.get("removed_at"),cleanup_status=plan.get("cleanup_status"),task_label=plan.get("task_label"))
        if "manifest" not in plan:
            return basic
        return basic | {
            "digest": plan["manifest"]["digest"], "summary": plan["manifest"]["summary"],
            "local": [{"path": row["path"], "files": row["file_count"], "bytes": row["bytes"]} for row in plan["manifest"]["local"]],
            "remote": [{"path": row["path"], "files": row["file_count"], "bytes": row["bytes"]} for row in plan["manifest"]["remote"]]}

    def reconcile(self, task_id, plan_id, actor, bearer):
        """Continue only after a fresh read proves every partial step is exact or absent."""
        plan = self.store.deletion(plan_id)
        if not plan or plan["task_id"] != task_id or plan["actor_sub"] != actor or plan["status"] != "unknown":
            raise ValueError("没有可对账的删除事务")
        if plan.get("manifest",{}).get("schema") == "wrf-task-directory-deletion-2":
            return self._reconcile_directory(task_id,plan_id,actor,bearer)
        if not self.store.deletion_blocked(task_id) or not self.store.resume_deletion(plan_id):
            raise ValueError("删除事务身份已变化")
        manifest = plan["manifest"]
        engine, _ = init_database(import_users=True)
        try:
            if manifest.get("schema") == "wrf-task-directory-deletion-2":
                raise ValueError("目录移除结果待确认，禁止重放旧文件删除流程")
            terminal_request = manifest.get("terminal_request")
            if not terminal_request:
                raise RuntimeError("旧删除计划缺少可复核的进程身份，禁止继续")
            control = remote_call(self.client,"scan",[terminal_request["control"]])
            if len(control) != 1 or control[0].get("path") != terminal_request["control"]:
                raise RuntimeError("远端控制目录核验不完整")
            evidence = remote_call(self.client,"terminal",[terminal_request],timeout=60) if control[0]["exists"] else remote_call(self.client,"session_absent",[manifest["terminal"]],timeout=60)
            if len(evidence) != 1 or not evidence[0].get("verified"):
                raise RuntimeError("删除对账前无法证明原计算会话已经退出")
            catalog = catalog_snapshot(engine,[task_id])[0]
            original = manifest["catalog"][0]
            retired = copy.deepcopy(original)
            if retired["record"]: retired["record"]["is_deleted"] = 1
            for asset in retired["assets"]: asset["asset_status"] = "retired"
            if catalog == original:
                catalog_snapshot(engine,[task_id],revoke=True,expected=manifest["catalog"])
            elif catalog != retired and (catalog["record"] or catalog["assets"]):
                raise RuntimeError("产品登记与批准清单不符，停止对账")
            self.store.advance_deletion(plan_id,"catalog_revoked")
            access = observe_static_access(manifest["catalog"],bearer)
            if any(item["status"] not in {403,404} for item in access):
                raise RuntimeError("撤销登记后仍可读取产品或无法认证访问")
            self.store.advance_deletion(plan_id,"access_denied")
            roots = [item["path"] for item in manifest["remote"]]
            observed = remote_call(self.client,"scan",roots)
            by_path = {item["path"]:item for item in observed}
            if len(by_path) != len(roots): raise RuntimeError("远端清单对账不完整")
            pending = []
            for original_root in manifest["remote"]:
                now = by_path[original_root["path"]]
                if now["exists"]:
                    if not remaining_valid(original_root,now): raise RuntimeError("远端出现非清单内变更，停止继续")
                    pending.append({"path":now["path"],"snapshot_sha256":sha(now)})
            if pending:
                remote_call(self.client,"delete",pending)
            self.store.advance_deletion(plan_id,"remote_deleted")
            for root in manifest["local"]:
                now = scan_root(root["path"])
                if now["exists"]:
                    if not remaining_valid(root,now): raise RuntimeError("本地出现非清单内变更，停止继续")
                    delete_root(now)
            self.store.advance_deletion(plan_id,"local_deleted")
            current_catalog = catalog_snapshot(engine,[task_id])[0]
            if current_catalog["record"] or current_catalog["assets"]:
                catalog_snapshot(engine,[task_id],purge=True,expected=manifest["catalog"])
            self.store.advance_deletion(plan_id,"catalog_purged")
            if self.store.get(task_id): self.store.purge_deleted_task(task_id,plan_id)
            return self.status(task_id,plan_id,actor)
        except Exception:
            current = self.store.deletion(plan_id)
            self.store.advance_deletion(plan_id,current["phase"],status="unknown")
            raise

    def _quarantine_assets(self, task_id, plan_id, manifest, client, engine):
        moves = {kind:[directories.intent(item,plan_id) for item in manifest[kind]] for kind in ("local","remote")}
        self.store.update_deletion_detail(plan_id,status="executing",phase="quarantining",quarantine=moves)
        directories.remote(client,"quarantine",moves["remote"])
        for item in moves["local"]:
            directories.quarantine(item)
        catalog_snapshot(engine,[task_id],purge=True,expected=manifest["catalog"])
        self.store.purge_deleted_task(task_id,plan_id,cleanup_pending=True)
        self.queue.cleanup_submit(lambda: self._purge_quarantine(plan_id,moves))

    def _directory_reconciliation_proof(self, task_id, plan_id, client, bearer):
        plan = self.store.deletion(plan_id)
        manifest = plan["manifest"]
        with self.store._lock:
            row = self.store._db.execute("SELECT deletion_plan_id,status FROM wrf_task_tombstones WHERE task_id=?",(task_id,)).fetchone()
        if not row or row["deletion_plan_id"] != plan_id or row["status"] != "deleting":
            raise ValueError("原删除事务墓碑身份已变化")
        if plan.get("removed") or (plan["phase"] != "quarantining" and not plan.get("reconciliation_requested")):
            raise ValueError("此事务不属于可证明尚未移走的目录删除，禁止重放")
        moves = plan.get("quarantine")
        expected = {kind:[directories.intent(item,plan_id) for item in manifest[kind]] for kind in ("local","remote")}
        if moves != expected:
            raise ValueError("原移动意图不完整或已变化")
        locations = directories.remote(client,"location",moves["remote"])
        locations += [directories.location(item) for item in moves["local"]]
        if len(locations) != len(moves["remote"])+len(moves["local"]) or any(state not in {"unmoved","absent"} for state in locations):
            raise ValueError("存在已移动、丢失或冲突目录，禁止重复执行")
        current = inventory(self.settings,self.store,client,task_id,directory_mode=True)
        if (current["task_fingerprint"] != manifest["task_fingerprint"]
                or current["local"] != manifest["local"] or current["remote"] != manifest["remote"]):
            raise ValueError("任务状态、运行身份或目录归属已变化")
        retired = copy.deepcopy(manifest["catalog"])
        for entry in retired:
            if entry.get("record"):
                entry["record"]["is_deleted"] = 1
            for asset in entry["assets"]:
                asset["asset_status"] = "retired"
        if current["catalog"] != retired:
            raise ValueError("原产品撤销登记与对账状态不符")
        if not bearer:
            raise ValueError("缺少当前管理员或任务所有者认证")
        if any(item["status"] not in {403,404} for item in observe_static_access(manifest["catalog"],bearer)):
            raise ValueError("原产品撤销后的认证访问仍未拒绝")
        return {"verified_at":time.time(),"locations":locations,"original_digest":manifest["digest"]}

    def _reconcile_directory(self, task_id, plan_id, actor, bearer):
        # Only explicit authenticated reconciliation can continue this same ID.
        client = _DeadlineClient(self.client)
        proof = self._directory_reconciliation_proof(task_id,plan_id,client,bearer)
        if not self.store.reserve_directory_reconciliation(plan_id,task_id,actor,proof):
            raise ValueError("原删除事务已处理或身份已变化")
        def run():
            with self._operation_lock(task_id):
                client = self._clients[plan_id]
                self._directory_reconciliation_proof(task_id,plan_id,client,bearer)
                client.allow_retries = False
                engine,_ = init_database(import_users=True)
                self._quarantine_assets(task_id,plan_id,self.store.deletion(plan_id)["manifest"],client,engine)
        try:
            self._enqueue(plan_id,run)
        except Exception:
            self.store.update_deletion_detail(plan_id,status="unknown",phase="quarantining")
            raise
        return self.status(task_id,plan_id,actor)

    def _purge_quarantine(self, plan_id, moves):
        try:
            self.store.update_deletion_detail(plan_id,cleanup_status="running",cleanup_started_at=time.time())
            client = _DeadlineClient(self.client)
            client.allow_retries = False
            directories.remote(client,"purge",moves["remote"],timeout=1800)
            for item in moves["local"]:
                directories.purge(item)
            self.store.update_deletion_detail(plan_id,status="complete",phase="complete",
                cleanup_status="complete",finished_at=time.time())
        except Exception as exc:
            logging.getLogger(__name__).exception("Quarantine cleanup %s failed",plan_id)
            self.store.update_deletion_detail(plan_id,status="unknown",phase="space_cleanup",
                cleanup_status="unknown",finished_at=time.time(),error={"category":deletion_error_category(exc),
                "message":"已移除，空间清理状态待确认；未重放操作。"})

    def execute(self, task_id, plan_id, actor, digest, bearer, _reserved=False):
        plan = self.store.deletion(plan_id)
        if not plan or plan["task_id"] != task_id or plan["actor_sub"] != actor:
            raise ValueError("删除计划不存在或不属于当前操作员")
        if plan["status"] != "pending" or time.time() > plan["expires_at"]:
            raise ValueError("计划已失效；请查询操作状态并重新预览")
        if plan.get("execution_requested") and not _reserved:
            raise ValueError("删除已受理，请查询原操作")
        manifest = plan["manifest"]
        if digest != manifest["digest"]:
            raise ValueError("清单摘要不一致")
        client = self._clients.get(plan_id) or (_DeadlineClient(self.client) if _reserved else self.client)
        if _reserved:self.store.update_deletion_detail(plan_id,phase="checking_execution")
        current = inventory(self.settings, self.store, client, task_id, directory_mode=manifest.get("schema")=="wrf-task-directory-deletion-2")
        if current["digest"] != digest:
            raise ValueError("任务、引用或文件已变化；请重新预览")
        if not bearer:
            raise ValueError("缺少用于正式资源访问核验的当前登录凭据")
        observed = observe_static_access(manifest["catalog"], bearer)
        if any(item["status"] not in {200, 403, 404} for item in observed):
            raise RuntimeError("正式资源访问无法验证")
        if isinstance(client,_DeadlineClient):client.allow_retries=False
        if not self.store.claim_deletion(plan_id, task_id):
            raise ValueError("任务状态已变化或已有删除事务")
        engine, _ = init_database(import_users=True)
        try:
            catalog_snapshot(engine, [task_id], revoke=True, expected=manifest["catalog"])
            self.store.advance_deletion(plan_id, "catalog_revoked")
            observed = observe_static_access(manifest["catalog"], bearer)
            if any(item["status"] not in {403, 404} for item in observed):
                raise RuntimeError("登记撤销后仍可读取静态资源，停止物理删除")
            self.store.advance_deletion(plan_id, "access_denied")
            if manifest.get("schema") == "wrf-task-directory-deletion-2":
                self._quarantine_assets(task_id,plan_id,manifest,client,engine)
                return self.status(task_id,plan_id,actor)
            # The remote script compares each complete snapshot before removing it.
            remote_call(client, "delete", [{"path": item["path"], "snapshot_sha256": sha(item)} for item in manifest["remote"]])
            self.store.advance_deletion(plan_id, "remote_deleted")
            for item in manifest["local"]:
                delete_root(item,deadline=client.deadline) if isinstance(client,_DeadlineClient) else delete_root(item)
            self.store.advance_deletion(plan_id, "local_deleted")
            catalog_snapshot(engine, [task_id], purge=True, expected=manifest["catalog"])
            self.store.advance_deletion(plan_id, "catalog_purged")
            self.store.purge_deleted_task(task_id, plan_id)
            return self.status(task_id, plan_id, actor)
        except Exception:
            current = self.store.deletion(plan_id)
            self.store.advance_deletion(plan_id, current["phase"], status="unknown")
            raise


def deletion_error_category(exc):
    detail=str(exc).lower()
    if isinstance(exc,HpcAuthError):return 'authentication'
    if isinstance(exc,TimeoutError) or any(x in detail for x in ('timeout','timed out','超时')):return 'timeout'
    if any(x in detail for x in ('identity','身份','managed launch','终态','launch evidence')):return 'identity'
    if any(x in detail for x in ('reference','引用')):return 'reference'
    if any(x in detail for x in ('changed','变化','清单')):return 'changed'
    if 'traceback' in detail:return 'unverified'
    return 'connection' if isinstance(exc,HpcError) else 'unverified'


class _DeadlineClient:
    """Budget starts at execution, with retries restricted to read-only calls."""
    def __init__(self, client):
        self.client = client
        self.deadline = time.monotonic()+1800
        self.allow_retries=True

    def __getattr__(self, name):
        value=getattr(self.client,name)
        return MethodType(value.__func__,self) if isinstance(value,MethodType) else value

    def run(self, *args, **kwargs):
        readonly=kwargs.pop('_readonly',True)
        stage_budget=kwargs.pop('_stage_budget',30)
        for attempt in range(3):
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Deletion execution deadline exceeded')
            attempt_kwargs={**kwargs,'timeout':min(float(kwargs.get('timeout',30)),stage_budget,remaining)}
            try:
                return self.client.run(*args,**attempt_kwargs)
            except (HpcError,TimeoutError) as exc:
                if not readonly or not self.allow_retries or attempt==2:raise
                if deletion_error_category(exc) not in {'timeout','connection'}:raise
                delay=(2,5)[attempt]
                if self.deadline-time.monotonic()<=delay:raise TimeoutError('Deletion execution deadline exceeded') from exc
                time.sleep(delay)

    def run_hash(self, command, timeout):
        return self.run(command,timeout=timeout,_stage_budget=min(900,max(60,timeout)))

    def remote(self, command, operation, payload, timeout):
        budget=30
        if operation in {'scan','delete'}:
            if operation=='scan':
                metadata=remote_call(self,'deletion-metadata',payload)
                size=sum(item['bytes'] for item in metadata)
            else:
                metadata=remote_call(self,'deletion-metadata',[item['path'] for item in payload])
                size=sum(item['bytes'] for item in metadata)
            budget=min(900,max(60,30+size/(10*1024*1024)))
        return self.run(command,timeout=budget,_stage_budget=budget,_readonly=operation!='delete')
