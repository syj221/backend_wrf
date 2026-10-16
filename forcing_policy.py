"""Versioned ordinary forcing policy and task-scoped download operations."""
from __future__ import annotations

import re
from uuid import uuid4, uuid5, NAMESPACE_URL
import json

from database import utc_now
from forcing_lifecycle import forcing_reference_guard

CYCLE = re.compile(r"^[0-9]{8}00$")


def policy_update(store, manager, client, settings, *, version, enabled, auto_cleanup, held_cycles):
    if auto_cleanup and not enabled:
        raise ValueError("自动清理只能随自动预取启用")
    if set(held_cycles) != {"gfs", "ecmwf"}:
        raise ValueError("必须明确列出 GFS、ECMWF 两源保留周期")
    for source, cycles in held_cycles.items():
        if not isinstance(cycles, list) or len(cycles) > 100 or len(set(cycles)) != len(cycles) or any(not isinstance(x,str) or not CYCLE.fullmatch(x) for x in cycles):
            raise ValueError(f"{source} 显式保留周期格式无效")
    if enabled:
        if not settings.hpc_workbench_data_root or not all(str(getattr(settings,name)).startswith(settings.hpc_workbench_data_root.rstrip("/")+"/forcing/") for name in ("hpc_gfs_dir","hpc_ec_dir")):
            raise ValueError("GFS、ECMWF 必须位于独立普通 WRF 资料池")
        lines = client.run("crontab -l", timeout=20).splitlines()
        if any("wrf_server_cron.sh" in line and line.strip() and not line.lstrip().startswith("#") for line in lines):
            raise ValueError("旧 WRF cron 仍启用，不能启用双调度入口")
    with forcing_reference_guard(store.path):
        saved = store.save_forcing_policy(version,enabled,auto_cleanup,held_cycles,settings.hpc_gfs_prefetch_enabled)
    manager.set_forcing_policy(saved)
    return saved


def request_download(store, client, actor, source, cycle, first, last, key=None):
    if source not in {"gfs","ecmwf"} or not CYCLE.fullmatch(cycle):
        raise ValueError("仅支持 GFS/ECMWF 的 00 UTC 资料周期")
    if type(first) is not int or type(last) is not int or first<0 or last>72 or first>last:
        raise ValueError("时次范围必须位于 f000–f072")
    step = 1 if source == "gfs" else 3
    if first%step or last%step:
        raise ValueError("ECMWF 时次必须按 3 小时间隔对齐")
    if key is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",key):
        raise ValueError("下载幂等键格式无效")
    identity = "forcing_"+(uuid5(NAMESPACE_URL,json.dumps(["wrf-forcing-1",actor,key])).hex if key else uuid4().hex)
    operation={"id":identity,"owner_sub":actor,"source":source,"cycle":cycle,
               "start_hour":first,"end_hour":last,"status":"submitting","created_at":utc_now(),
               "updated_at":utc_now(),"detail":{"hours":list(range(first,last+1,step))}}
    with forcing_reference_guard(store.path):
        before = store.forcing_operation(identity)
        if before:
            if [before[k] for k in ("owner_sub","source","cycle","start_hour","end_hour")] != [actor,source,cycle,first,last]:
                raise ValueError("同一下载幂等键绑定的资料范围不同")
            return before
        store.record_forcing_operation(operation)
    try:
        result=client.trigger_forcing_download(source,cycle,last,operation["detail"]["hours"],reference=identity)
    except Exception:
        store.update_forcing_operation(operation["id"],"unknown",{"message":"下载提交结果不明，请只读核验资料池和远端队列"})
        return store.forcing_operation(operation["id"])
    store.update_forcing_operation(operation["id"],"accepted",result)
    return store.forcing_operation(operation["id"])


def cancel_download(store, client, operation):
    with forcing_reference_guard(store.path):
        operation = store.forcing_operation(operation["id"])
        if operation["status"] == "submitting":
            raise ValueError("下载提交仍在对账，暂不能取消；请稍后核验")
        if operation["status"] not in {"accepted","unknown","cancel_pending","cancel_unknown"}:
            return operation
        store.update_forcing_operation(operation["id"],"cancel_pending",{})
    try:
        client.release_forcing_reference(operation["source"],operation["cycle"],operation["id"])
    except Exception:
        store.update_forcing_operation(operation["id"],"cancel_unknown",{"message":"引用释放结果待核验，资料继续受保护"})
        return store.forcing_operation(operation["id"])
    store.update_forcing_operation(operation["id"],"cancelled",{
        "message":"已释放本请求引用；其他任务、预取及已在途下载不受影响"})
    return store.forcing_operation(operation["id"])


def observe_download(store, client, operation):
    """Reconcile a request with files and the existing queue, never enqueue."""
    if operation["status"] == "ready":
        return {**operation, "observation": {
            "status": "ready", "valid_hours": operation["detail"].get("valid_hours", []),
            "missing_hours": [], "checked_at": operation["updated_at"]}}
    if operation["status"] not in {"accepted", "unknown"}:
        return operation
    from hpc import HpcError
    hours = operation["detail"].get("hours") or list(range(
        operation["start_hour"], operation["end_hour"] + 1,
        1 if operation["source"] == "gfs" else 3))
    try:
        files = client.inspect_forcing_files(operation["source"], operation["cycle"], hours)
        observed = {"valid_hours": files.get("valid_hours", []),
                    "missing_hours": files.get("missing_hours", []), "checked_at": utc_now()}
        if not files.get("complete"):
            observed.update(client.forcing_download_progress(
                operation["source"], operation["cycle"], observed["missing_hours"] or hours))
            if observed.get("untracked_hours"):
                observed["message"] = "部分时次没有队列记录；请核验后补缺，不会自动重放请求"
    except HpcError:
        observed = {"status": "unknown", "message": "暂不可核验；不会重新提交下载", "checked_at": utc_now()}
        files = {}
    # Cancellation may complete during SSH reads. Never overwrite its state.
    with forcing_reference_guard(store.path):
        current = store.forcing_operation(operation["id"])
        if current["status"] not in {"accepted", "unknown"}:
            return current
        if files.get("complete"):
            store.update_forcing_operation(operation["id"], "ready", {
                "valid_hours": observed["valid_hours"], "missing_hours": []})
            current = store.forcing_operation(operation["id"])
            observed["status"] = "ready"
        return {**current, "observation": observed}
