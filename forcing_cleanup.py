"""Actor-bound, expiring manifests for explicit forcing cleanup."""
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from forcing_lifecycle import forcing_reference_guard
from retirement import sha
from deletion_queue import DeletionQueue, timings
from task_deletion import deletion_error_category, _DeadlineClient
from hpc_transport import HpcCleanupBlockedError
import directory_delete as directories


class ForcingCleanupService:
    def __init__(self, manager, deletion_queue=None):
        self.manager = manager
        self.store = manager.store
        self._refresh_lock = threading.Lock()
        self._refresh_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wrf-pool-refresh")
        self._refresh = None
        self._cycle_lock = threading.Lock()
        self.queue=deletion_queue or DeletionQueue()
        self._owns_queue=deletion_queue is None
        self._cycle_worker=self.queue.worker

    def close(self):
        self._refresh_worker.shutdown(wait=True, cancel_futures=True)
        if self._owns_queue:self.queue.close()

    def _cycle_candidate(self, source, cycle, client=None):
        manager = self.manager
        manager.refresh_forcing_terminal_proofs(client) if client is not None else manager.refresh_forcing_terminal_proofs()
        targets = manager.gfs.latest_cycles(count=2,data_source=source)
        policy = self.store.forcing_policy(manager.settings.hpc_gfs_prefetch_enabled)
        protected = (set(manager.task_forcing_reference_counts(source,release_terminal=True))
                     | self.store.forcing_download_cycles(source) | set(policy["held_cycles"][source]))
        pool = (client or manager.hpc).forcing_pool_items(source,targets,protected)[0]
        item = next((item for item in pool.get("cycles",[]) if item["cycle"]==cycle),None)
        if not item or not item.get("cleanup_allowed"):
            raise ValueError("该周期属于最新资料、保留周期或仍有任务/下载引用，暂不能删除。")
        return item

    def delete_cycle_async(self, source, cycle, actor):
        import re
        if source not in {"gfs","ecmwf"} or not re.fullmatch(r"[0-9]{8}00",cycle):
            raise ValueError("资料源或周期格式无效")
        with self._cycle_lock:
            with self.store._lock:
                rows = self.store._db.execute(
                    "SELECT id FROM wrf_forcing_cleanup_plans WHERE status IN ('checking','executing','unknown') OR (status='blocked' AND json_extract(data_json,'$.retryable_before_write')=1) ORDER BY rowid DESC",()).fetchall()
            retry=None
            for row in rows:
                before = self.store.forcing_cleanup_plan(row["id"])
                if before.get("cycle")==cycle and before.get("source")==source:
                    if before['status']=='blocked' and before['actor_sub']!=actor:
                        continue
                    if before["actor_sub"]!=actor:raise ValueError("该周期已有删除操作正在处理")
                    if before['status']=='blocked' and not before.get('removed'):
                        retry=before;break
                    return self.public(before)
            plan = {"id":"cycle_delete_"+uuid4().hex,"actor_sub":actor,"source":source,"cycle":cycle,
                    "status":"checking","phase":"queued","created_at":time.time()}
            if retry:
                plan=retry
                history=list(plan.get('retry_history') or [])
                history.append({'finished_at':plan.get('finished_at'),'error':plan.get('error')})
                self.store.update_forcing_cleanup_detail(plan['id'],status='checking',phase='queued',error=None,
                    retry_history=history[-20:],retryable_before_write=False,finished_at=None)
            else:
                self.store.save_forcing_cleanup_plan(plan)
            def run():
                executing=False
                client=_DeadlineClient(self.manager.hpc)
                try:
                    self.store.update_forcing_cleanup_detail(plan["id"],phase="checking_references")
                    with forcing_reference_guard(self.manager.settings.database_path):
                        candidate=self._cycle_candidate(source,cycle,client)
                    self.store.update_forcing_cleanup_detail(plan["id"],phase="inventory_assets")
                    snapshots=directories.remote(client,"identity",[candidate["remote_path"]])
                    if len(snapshots)!=1 or snapshots[0]["path"]!=candidate["remote_path"]:
                        raise ValueError("资料目录身份不完整，未删除")
                    move=directories.intent(snapshots[0],plan["id"])
                    with forcing_reference_guard(self.manager.settings.database_path):
                        current=self._cycle_candidate(source,cycle,client)
                        if current["remote_path"]!=candidate["remote_path"]:
                            raise ValueError("资料目录发生变化，未删除")
                        self.store.update_forcing_cleanup_detail(plan["id"],status="executing",phase="quarantining",quarantine=move)
                        executing=True
                        client.allow_retries=False
                        directories.forcing_quarantine(client,source,move)
                        self.store.update_forcing_cleanup_detail(plan["id"],removed=True,removed_at=time.time(),
                            phase="space_cleanup",cleanup_status="pending")
                    self.queue.cleanup_submit(lambda:self._purge_cycle(plan["id"],move))
                except Exception as exc:
                    logging.getLogger(__name__).exception("Cycle deletion %s failed",plan["id"])
                    uncertain=executing and not isinstance(exc,HpcCleanupBlockedError)
                    message="资料下载正在占用资源，未删除；请稍后再试。" if isinstance(exc,HpcCleanupBlockedError) else ("删除结果待确认，已保留原操作。" if uncertain else (str(exc) if isinstance(exc,ValueError) else "资料暂不能删除，后台已保留处理记录。"))
                    self.store.update_forcing_cleanup_detail(plan["id"],status="unknown" if uncertain else "blocked",
                        retryable_before_write=isinstance(exc,HpcCleanupBlockedError),
                        error={"message":message,"category":deletion_error_category(exc)},finished_at=time.time())
            def started(now):
                self.store.update_forcing_cleanup_detail(plan["id"],started_at=now,phase="checking_references")
            try:
                self.queue.submit(plan["id"],run,started)
            except Exception:
                self.store.update_forcing_cleanup_detail(plan["id"],status="blocked",phase="queue_full")
                raise
            return self.public(plan)

    def _purge_cycle(self, identifier, move):
        try:
            self.store.update_forcing_cleanup_detail(identifier,cleanup_status="running",cleanup_started_at=time.time())
            client=_DeadlineClient(self.manager.hpc)
            client.allow_retries=False
            directories.remote(client,"purge",move if isinstance(move,list) else [move],timeout=1800)
            self.store.update_forcing_cleanup_detail(identifier,status="complete",phase="complete",
                cleanup_status="complete",finished_at=time.time())
        except Exception as exc:
            logging.getLogger(__name__).exception("Cycle space cleanup %s failed",identifier)
            self.store.update_forcing_cleanup_detail(identifier,status="unknown",phase="space_cleanup",
                cleanup_status="unknown",finished_at=time.time(),error={"category":deletion_error_category(exc),
                "message":"已移除，空间清理状态待确认；未重放操作。"})

    def refresh_status(self):
        with self._refresh_lock:
            return dict(self._refresh or {"status":"idle"})

    def refresh_async(self, actor, role):
        """Accept one refresh; the existing manager still owns all downloads."""
        with self._refresh_lock:
            if self._refresh and self._refresh["status"] == "accepted":
                return dict(self._refresh)
            operation = {"id":"pool_refresh_"+uuid4().hex,"status":"accepted"}
            self._refresh = operation
            def prepare():
                try:
                    from forcing_policy import policy_update
                    policy = self.store.forcing_policy(self.manager.settings.hpc_gfs_prefetch_enabled)
                    if role >= 3:
                        # The refresh action authorizes the two-cycle policy.
                        # Keep explicit holds and validate the sole scheduler.
                        policy_update(self.store,self.manager,self.manager.hpc,self.manager.settings,
                            version=policy["version"],enabled=True,auto_cleanup=True,
                            held_cycles=policy["held_cycles"])
                        for source in ("gfs","ecmwf"):
                            self.store.approve_retention(source,operation["id"])
                    elif not policy["enabled"] or not policy["auto_cleanup"] or not all(
                            self.store.retention_approved(source) for source in ("gfs","ecmwf")):
                        raise ValueError("请管理员首次刷新以启用两周期资料管理")
                    self.manager._forcing_cleanup_wakeup.set()
                    with self._refresh_lock:
                        self._refresh = {**operation,"status":"scheduled"}
                except Exception:
                    logging.getLogger(__name__).exception("Pool refresh %s failed",operation["id"])
                    with self._refresh_lock:
                        self._refresh = {**operation,"status":"blocked",
                            "message":"资料刷新未启动，请管理员检查连接或资料策略。"}
            try:
                self._refresh_worker.submit(prepare)
            except Exception:
                self._refresh = {**operation,"status":"blocked"}
                raise
            return dict(operation)

    def preview(self, source, retention_upgrade=False):
        manager = self.manager
        source = manager.gfs.normalize_source(source)
        targets = manager.gfs.latest_cycles(count=2,data_source=source)
        policy = self.store.forcing_policy(manager.settings.hpc_gfs_prefetch_enabled)
        if retention_upgrade:
            manager.refresh_forcing_terminal_proofs()
            protected = set(manager.task_forcing_reference_counts(source,release_terminal=True)) | self.store.forcing_download_cycles(source)
        else:
            protected = manager.active_forcing_cycles(source)
        protected |= set(policy["held_cycles"][source])
        if source == "gfs":
            protected |= manager.retained_gfs_cycles()
        pool = manager.hpc.forcing_pool_items(source,targets,protected)[0]
        enough = len(pool.get("retained_complete_cycles") or []) >= 2
        return {"source":source,"targets_complete":pool.get("targets_complete"),
                "retention_upgrade":retention_upgrade,
                "retained_complete_cycles":pool.get("retained_complete_cycles"),
                "policy_version":policy["version"],
                "candidates":[item for item in pool.get("cycles") or [] if item.get("cleanup_allowed") and (not retention_upgrade or enough)],
                "protected":[item for item in pool.get("cycles") or [] if item.get("protected") or item.get("retained_complete")],
                "automatic_cleanup_enabled":policy["auto_cleanup"]}

    def plan(self, source, actor, retention_upgrade=False, *, directory_mode=True):
        with forcing_reference_guard(self.manager.settings.database_path):
            preview = self.preview(source,retention_upgrade)
            if retention_upgrade and len(preview.get("retained_complete_cycles") or []) < 2:
                raise ValueError("尚不足两个完整周期，暂不启用扩展清理规则")
            paths = [row["remote_path"] for row in preview["candidates"]]
        # Only the root identity is read for new plans. Frozen legacy manifests
        # keep their original byte-fingerprint contract.
        snapshots = (directories.remote(self.manager.hpc,"identity",paths) if directory_mode else self.manager.hpc.snapshot_forcing_cycles(preview["source"],paths))
        with forcing_reference_guard(self.manager.settings.database_path):
            current = self.preview(source,retention_upgrade)
            allowed = {row["remote_path"] for row in current["candidates"]}
            if (preview["policy_version"] != current["policy_version"] or not set(paths).issubset(allowed)
                    or (retention_upgrade and len(current.get("retained_complete_cycles") or []) < 2)):
                raise ValueError("资料引用或保留策略在核验期间变化，请重新预览")
            if {item["path"] for item in snapshots} != set(paths) or len(snapshots) != len(paths):
                raise ValueError("资料核验清单不完整")
            by_path = {item["path"]:item for item in snapshots}
            preview["candidates"] = preview["candidates"] if directory_mode else [{**item,"file_count":by_path[item["remote_path"]].get("files"),
                "size_bytes":by_path[item["remote_path"]].get("bytes",item.get("size_bytes",0))}
                for item in preview["candidates"]]
            plan = {**preview,"id":"forcing_cleanup_"+uuid4().hex,"actor_sub":actor,
                    "status":"pending","created_at":time.time(),"expires_at":time.time()+600,
                    "snapshots":snapshots,"directory_mode":directory_mode}
            plan["digest"] = sha(plan)
            self.store.save_forcing_cleanup_plan(plan)
        return self.public(plan)

    def _execute_directory_plan(self, plan, actor):
        identifier=plan["id"]
        if not self.store.update_forcing_cleanup_plan(identifier,"checking",expected="pending"):
            raise ValueError("资料清理已被处理，不得重复执行")
        self.store.update_forcing_cleanup_detail(identifier,phase="queued")
        def run():
            writing=False
            try:
                client=_DeadlineClient(self.manager.hpc)
                with forcing_reference_guard(self.manager.settings.database_path):
                    current=self.preview(plan["source"],plan.get("retention_upgrade",False))
                    paths=[item["path"] for item in plan["snapshots"]]
                    allowed={item["remote_path"] for item in current["candidates"]}
                    if current["policy_version"]!=plan["policy_version"] or not set(paths).issubset(allowed):
                        raise ValueError("资料引用、保留策略或可清理范围已变化")
                    if directories.remote(client,"identity",paths)!=plan["snapshots"]:
                        raise ValueError("资料目录身份已变化")
                    moves=[directories.intent(item,identifier) for item in plan["snapshots"]]
                    self.store.update_forcing_cleanup_detail(identifier,status="executing",phase="quarantining",quarantine=moves)
                    client.allow_retries=False
                    for move in moves:
                        try:
                            directories.forcing_quarantine(client,plan["source"],move)
                        except HpcCleanupBlockedError:
                            raise
                        except Exception:
                            writing=True
                            raise
                        writing=True
                    if plan.get("retention_upgrade"):
                        self.store.approve_retention(plan["source"],identifier)
                    self.store.update_forcing_cleanup_detail(identifier,removed=True,removed_at=time.time(),phase="space_cleanup",cleanup_status="pending")
                self.queue.cleanup_submit(lambda:self._purge_cycle(identifier,moves))
            except Exception as exc:
                logging.getLogger(__name__).exception("Batch cycle removal %s failed",identifier)
                self.store.update_forcing_cleanup_detail(identifier,status="unknown" if writing else "blocked",finished_at=time.time(),
                    error={"category":deletion_error_category(exc),"message":"资料移除结果待确认，未重放操作。" if writing else "资料目录或引用条件不满足，未删除。"})
        def started(now):
            self.store.update_forcing_cleanup_detail(identifier,started_at=now,phase="checking_references")
        try:
            self.queue.submit(identifier,run,started)
        except Exception:
            self.store.update_forcing_cleanup_detail(identifier,status="blocked",phase="queue_full")
            raise
        return self.status(identifier,actor)

    def public(self,plan):
        result={key:value for key,value in plan.items() if key not in {"actor_sub","snapshots","quarantine"}}
        result.update(timings(plan,self.queue.position(plan["id"])))
        result["elapsed_seconds"]=round(max(0,plan.get("finished_at",time.time())-plan["created_at"]),1)
        return result

    def status(self, identifier, actor):
        plan = self.store.forcing_cleanup_plan(identifier)
        if not plan or plan["actor_sub"] != actor:
            raise ValueError("资料清理计划不存在或不属于当前操作员")
        return self.public(plan)

    def execute(self, identifier, actor, digest):
        plan = self.store.forcing_cleanup_plan(identifier)
        if not plan or plan["actor_sub"] != actor:
            raise ValueError("资料清理计划不存在或不属于当前操作员")
        if plan["status"] != "pending" or time.time() > plan["expires_at"] or digest != plan["digest"]:
            raise ValueError("资料清理计划已失效或摘要不一致，请核验状态")
        if plan.get("directory_mode"):
            return self._execute_directory_plan(plan,actor)
        with forcing_reference_guard(self.manager.settings.database_path):
            current = self.preview(plan["source"],plan.get("retention_upgrade",False))
            paths = [item["path"] for item in plan["snapshots"]]
            allowed = {item["remote_path"] for item in current["candidates"]}
            if current["policy_version"] != plan["policy_version"] or not set(paths).issubset(allowed):
                raise ValueError("资料引用、保留策略或可清理范围已变化，请重新预览")
            if not self.store.update_forcing_cleanup_plan(identifier,"executing",expected="pending"):
                raise ValueError("资料清理已被处理，不得重复执行")
            try:
                if paths:
                    kwargs = {"retention_upgrade":True} if plan.get("retention_upgrade") else {}
                    self.manager.cleanup_forcing(plan["source"],paths,actor_sub=actor,
                        expected_snapshots=plan["snapshots"],_reference_guard_held=True,**kwargs)
                if plan.get("retention_upgrade"):
                    self.store.approve_retention(plan["source"],identifier)
                self.store.update_forcing_cleanup_plan(identifier,"complete")
            except Exception:
                self.store.update_forcing_cleanup_plan(identifier,"unknown")
                raise
        return self.status(identifier,actor)
