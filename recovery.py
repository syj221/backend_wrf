"""Evidence-based incidents and single-use, confirmed recovery plans."""
import hashlib
import json
import re
import time
import threading
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
from uuid import uuid4

from configuration_profile import normalize as normalize_common
from schemas import normalize_task_request
from scripts.namelist_layout import is_namelist_read_failure
from request_validation import execution_capability, validate_ordinary_request

TROUBLESHOOTING = "https://www2.mmm.ucar.edu/wrf/users/wrf_users_guide/build/html/troubleshooting.html"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def fingerprint(task):
    return digest({key: task.get(key) for key in ("id", "attempt_no", "status", "stage", "request", "failure")})


def validation_digest(result):
    plan = result.get("namelist_plan") or {}
    return digest([result["request"], plan.get("digest"), plan.get("previews")])


def user_configuration(request):
    value = deepcopy(request)
    if isinstance(value.get("namelist"), dict):
        value["namelist"].pop("plan_digest", None)
    return normalize_common(normalize_task_request(value))


def diagnose(task, evidence=""):
    evidence = re.sub(r"(?i)Bearer\s+\S+|(?:password|token|secret)\s*[:=]\s*\S+", "[redacted]", evidence)
    failure = task.get("failure") or {}
    message = re.sub(r"(?i)Bearer\s+\S+|(?:password|token|secret)\s*[:=]\s*\S+", "[redacted]", str(failure.get("message") or task.get("error") or ""))
    text = (message + "\n" + evidence).lower()
    code, confidence = str(failure.get("failure_class") or "unknown"), "medium"
    if is_namelist_read_failure(text) or code == "configuration_generation":
        code, confidence = "configuration_generation", "high"
        message = "系统生成的运行配置文件无法读取；请修复生成器，重新校验并确认计划，不要直接重试旧配置。"
    elif re.search(r"\bcfl\b.*(?:exceed|error|unstable)|points exceeded (?:v_)?cfl", text):
        code, confidence = "cfl", "high"
    elif "out of memory" in text or "oom-kill" in text:
        code, confidence = "memory", "high"
    elif "no space left" in text or task.get("stage") == "blocked_storage":
        code, confidence = "storage", "high"
    elif "segmentation fault" in text:
        code, confidence = "unknown", "low"
    elif failure.get("code") == "stalled":
        code, confidence = "stalled", "low"
    actions = []
    if (task.get("runtime") or {}).get("remote_wrf_succeeded"):
        actions = ["retry_outputs"]
    elif task.get("status") == "paused_external":
        actions = ["resume"]
    elif task.get("status") in {"failed", "waiting_restart", "cancelled"}:
        if code == "configuration_generation":
            actions = ["regenerate_plan"]
        elif code == "cfl":
            actions = ["reduce_time_step", "restart"]
        elif code in {"configuration", "memory", "data"}:
            actions = ["restart"]
    # A stalled or disconnected observation alone must never imply a dead process.
    return {"code": code, "confidence": confidence, "message": message or "模拟时间暂未推进，需核查远端进程和日志",
            "evidence": evidence[-12000:], "actions": actions, "source": TROUBLESHOOTING,
            "requires_confirmation": True}


SIGNALS = ("fio-f-228", "namelist read", "end of file reached without finding group",
           "namelist_layout_error", "points exceeded v_cfl", "points exceeded cfl",
           "segmentation fault", "out of memory", "oom-kill", "no space left on device")
_UNSET = object()


class RecoveryService:
    def __init__(self, manager, runtime_provider=None, runtime_verifier=None):
        self.manager = manager
        self.store = manager.store
        self.runtime_provider = runtime_provider or (lambda: None)
        self.runtime_verifier = runtime_verifier or self.runtime_provider
        self._diagnosis_cache = {}
        self._diagnosis_lock = threading.RLock()
        self._diagnosis_inflight = {}

    def diagnosis(self, task_id, event_id=None, attempt_no=None, *, runtime=_UNSET, force_refresh=False):
        # Repeated historic cards share one short-lived observation. Recovery
        # preparation/execution bypass the cache and recheck all safety gates.
        task = self.manager.get(task_id)
        key = (task_id, event_id, attempt_no, fingerprint(task))
        with self._diagnosis_lock:
            if len(self._diagnosis_inflight) >= 256:
                for old_key, old_lock in list(self._diagnosis_inflight.items()):
                    if not old_lock.locked():
                        self._diagnosis_inflight.pop(old_key, None)
                        self._diagnosis_cache.pop(old_key, None)
            lock = self._diagnosis_inflight.setdefault(key, threading.Lock())
        with lock:
            with self._diagnosis_lock:
                cached = self._diagnosis_cache.get(key)
                if not force_refresh and runtime is _UNSET and cached and cached[0] > time.monotonic():
                    return deepcopy(cached[1])
            result = self._diagnosis(task_id, event_id, attempt_no, runtime=runtime)
            with self._diagnosis_lock:
                if len(self._diagnosis_cache) >= 256:
                    stale = [item for item in self._diagnosis_cache
                             if item not in self._diagnosis_inflight or not self._diagnosis_inflight[item].locked()]
                    for item in stale:
                        self._diagnosis_cache.pop(item, None)
                        self._diagnosis_inflight.pop(item, None)
                self._diagnosis_cache[key] = (time.monotonic() + 20, deepcopy(result))
            return result

    def _diagnosis(self, task_id, event_id=None, attempt_no=None, *, runtime=_UNSET):
        """Read bounded evidence without rewriting incident or task history."""
        task = self.manager.get(task_id)
        attempt = int(attempt_no or task.get("attempt_no", 1))
        current = attempt == int(task.get("attempt_no", 1))
        events = self.store.incidents(task_id)
        event = next((e for e in events if e["attempt_no"] == attempt and
                      (e["id"] == event_id if event_id else e["status"] == "open")), None)
        if event_id and event is None:
            raise ValueError("事件与当前任务或尝试不匹配，请刷新诊断")
        if not current:
            history = next((item for item in self.store.attempts(task_id)
                            if int(item["attempt_no"]) == attempt), None)
            if history is None:
                raise ValueError("任务尝试不存在")
            task = {**history, "id": task_id}
        parts = [str((task.get("failure") or {}).get("evidence") or ""),
                 str((event or {}).get("evidence") or "")]
        sources = [{"source": "recorded_incident", "status": "available" if any(parts) else "missing"}]
        reader = getattr(self.manager, "diagnostic_evidence", None)
        observation = {}
        if reader:
            observation = reader(task_id, attempt)
            parts.extend(observation.get("evidence", []))
            sources.extend(observation.get("sources", []))
        evidence = "\n".join(str(part)[-24000:] for part in parts if part)
        diagnosis = diagnose(task, evidence)
        runtime = self.runtime_provider() if runtime is _UNSET else runtime
        capability = execution_capability(runtime)
        blockers, unknowns = [], []
        facts = []
        if evidence:
            facts.append("已核查本次尝试可取得的运行证据。")
        else:
            unknowns.append("当前没有可读取的故障日志，不能从退出码判断根因。")
        signals = [signal for signal in SIGNALS if signal in evidence.lower()]
        code = diagnosis["code"]
        messages = {
            "configuration_generation": "系统生成的运行配置文件读取失败；不是需要您猜测的气象参数问题。",
            "cfl": "日志出现数值稳定性告警，需要核验并调整积分步长后再计算。",
            "memory": "日志报告内存不足，需要确认资源或配置调整后再计算。",
            "storage": "运行存储空间不足，需要先处理存储条件。",
            "external": "外部连接中断，尚不能据此判断远端计算是否失败。",
            "output": "计算后的结果处理未完成，需要核验原计算结果。",
            "data": "驱动资料准备阶段失败，需要核验所需资料是否可用。",
            "configuration": "运行配置未通过检查，需要确认具体修正。",
        }
        message = messages.get(code, "任务未正常结束，当前证据不足以确定可安全执行的修复。")
        actions = list(diagnosis["actions"])
        if code == "configuration_generation":
            facts.append("已识别运行配置读取错误，不能直接重试旧生成文件。")
            unknowns.append("错误特征本身不能区分配置段顺序、缺段或其他格式问题。")
            if "regenerate_plan" in actions and not capability["verified"]:
                blockers.append("尚未确认当前生成器对应的 WRF/WPS 构建；恢复连接或部署修复后重新核验。")
                actions = []
        elif code in {"unknown", "model", "stalled"} and not actions:
            blockers.append("缺少可验证的修复依据，不能将原配置重跑作为修复方案。")
            if not actions:
                unknowns.append("需要补充故障日志或由维护人员确认原因。")
        if any(source["status"] == "unavailable" for source in sources):
            unknowns.append("部分运行证据暂时不可读取，保留已有证据；未将连接问题视为任务终止。")
        if not current:
            actions = []
            blockers.append("这是历史尝试，仅供查看；不能再次执行恢复。")
        remote_status = observation.get("remote_status", "not_checked")
        if current and remote_status in {"running", "lost", "unknown", "unavailable"}:
            actions = [action for action in actions if action in {"resume", "retry_outputs"}]
            note = "远端尚未核验为可重启终态；不会重复启动计算。"
            (unknowns if actions else blockers).append(note)
        if current and remote_status == "succeeded":
            actions = ["retry_outputs"] if (task.get("runtime") or {}).get("remote_wrf_succeeded") else []
            facts.append("远端报告计算已结束，应保留原计算并核验结果处理。")
            if not actions:
                blockers.append("本地尚未确认计算成功状态，需要核对原任务；禁止重新计算。")
        safe = {"schema_version": "wrf-diagnosis-1", "task_id": task_id,
                "event_id": (event or {}).get("id"), "attempt_no": attempt,
                "code": code, "confidence": diagnosis["confidence"], "message": message,
                "facts": facts, "unknowns": unknowns, "evidence_signals": signals,
                "evidence_sources": sources, "remote_status": remote_status,
                "evidence_digest": digest([code, diagnosis["confidence"], signals]),
                "blockers": blockers, "actions": actions, "requires_confirmation": True,
                "capability_version": capability["version"]}
        safe["diagnosis_digest"] = digest({key: safe[key] for key in (
            "task_id", "event_id", "attempt_no", "code", "confidence", "evidence_digest", "capability_version")})
        safe["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.store.save_diagnosis(safe)
        return safe

    def events(self, task_id):
        task = self.manager.get(task_id)
        if task.get("failure") and not any(e["attempt_no"] == task.get("attempt_no", 1) for e in self.store.incidents(task_id)):
            self.store.record_incident(task)
        return {"events": self.store.incidents(task_id)}

    def plan(self, task_id, event_id, action, proposed_request=None, actor_sub="", purpose="repair"):
        if purpose == "manual":
            return self._manual_plan(task_id, action, proposed_request, actor_sub)
        if purpose != "repair":
            raise ValueError("未知操作目的")
        if proposed_request is not None and action != "restart":
            raise ValueError("只有配置修正后重跑可以提供替代配置")
        task = self.manager.get(task_id)
        event = next((e for e in self.store.incidents(task_id) if e["id"] == event_id), None)
        if not event or event["attempt_no"] != task.get("attempt_no", 1) or event["status"] != "open":
            raise ValueError("事件已失效，请重新诊断")
        runtime = self.runtime_verifier()
        diagnosis = self.diagnosis(task_id, event_id, runtime=runtime, force_refresh=True)
        if diagnosis["code"] == "configuration_generation" and action == "restart":
            raise ValueError("配置读取故障须修正配置生成，使用 regenerate_plan 重新生成并校验，不能提交原配置重跑")
        if action not in diagnosis["actions"]:
            raise ValueError("；".join(diagnosis["blockers"]) or "当前证据和任务状态不允许此恢复动作")
        if action == "restart" and diagnosis["code"] in {"cfl", "configuration", "configuration_generation", "memory"} and not proposed_request:
            raise ValueError("此故障需要修正配置后再重跑；请提供修正配置或选择适用的时间步长修复")
        request = deepcopy(task["request"])
        paths = None
        validated = None
        if action in {"restart", "reduce_time_step", "regenerate_plan"}:
            paths = self.manager.restart_plan(task_id)
            if not paths["can_restart"]:
                raise ValueError(paths.get("reason") or "远端状态不允许重算")
            if proposed_request:
                request = proposed_request
                if request == task["request"]:
                    raise ValueError("替代配置没有任何修正，不能当作故障修复")
            if action == "reduce_time_step":
                current = int(request.get("time_step_seconds") or max(1, request["domains"][0]["dx"] // 1000 * 5))
                alignment = int(request.get("forecast_interval_hours", 1)) * 3600
                output = int(request.get("history_interval_minutes", 60)) * 60
                smaller = [n for n in range(current // 2, 0, -1) if alignment % n == 0 and output % n == 0]
                if not smaller:
                    raise ValueError("当前已没有更小且合法的整数积分步长，不能生成无修改的修复计划")
                request["time_step_seconds"] = smaller[0]
            validated = validate_ordinary_request(request, runtime=runtime,
                regenerate=action == "regenerate_plan" or (action == "reduce_time_step" and bool(request.get("namelist"))))
            request = validated["request"]
            if action == "restart" and proposed_request and user_configuration(request) == user_configuration(task["request"]):
                raise ValueError("规范化后的替代配置没有实际修正，不能当作故障修复")
        plan = {"schema_version": "wrf-recovery-2", "id": "repair_" + uuid4().hex, "task_id": task_id, "event_id": event_id,
                "attempt_no": task.get("attempt_no", 1), "fingerprint": fingerprint(task),
                "action": action, "request": request, "paths": paths, "status": "pending", "actor_sub": actor_sub,
                "expires_at": time.time() + 600, "diagnosis": diagnosis,
                "diagnosis_digest": diagnosis["diagnosis_digest"],
                "capability_version": diagnosis["capability_version"],
                "namelist_plan": validated["namelist_plan"] if validated else None,
                "validation_digest": validation_digest(validated) if validated else None,
                "changes": {key: {"before": task["request"].get(key), "after": value} for key, value in request.items() if task["request"].get(key) != value},
                "impact": "将重新计算并归档旧尝试；耗时和内存未校准" if paths else "从原阶段恢复，不重新计算已成功的 WRF"}
        self.store.save_recovery(plan)
        return plan

    def _manual_plan(self, task_id, action, proposed_request, actor_sub):
        if action not in {"resume", "retry_outputs", "render_partial", "restart"}:
            raise ValueError("不支持此手动操作")
        if proposed_request is not None and action != "restart":
            raise ValueError("只有重跑可以修改参数")
        task = self.manager.get(task_id)
        self.manager.require_operation(task_id, action,**({"allow_completed":True} if action=="restart" and proposed_request is not None and (task.get("runtime") or {}).get("remote_wrf_succeeded") else {}))
        request = deepcopy(proposed_request if proposed_request is not None else task["request"])
        completed = bool((task.get("runtime") or {}).get("remote_wrf_succeeded"))
        recompute = action == "restart" and completed and proposed_request is not None
        if action == "restart" and completed and not recompute:
            raise ValueError("计算已成功；请重新生成结果，或修改参数后明确确认重新计算")
        paths, validated = None, None
        if action == "restart":
            paths = self.manager.restart_plan(task_id,**({"allow_completed":True} if recompute else {}))
            if not paths["can_restart"]:
                raise ValueError(paths.get("reason") or "远端终态尚未核验")
            validated = validate_ordinary_request(request, runtime=self.runtime_verifier())
            request = validated["request"]
        impact = {"restart": "归档旧尝试后重新计算；失败根因可能尚未确定，再次运行仍可能失败",
                  "resume": "恢复连接并核验原运行状态，不启动第二次计算",
                  "retry_outputs": "仅重新获取和处理结果，不重新计算 WRF",
                  "render_partial": "忽略已确认的不可读帧，仅生成部分结果，不登记为完整产品"}[action]
        plan = {"schema_version": "wrf-recovery-2", "id": "repair_" + uuid4().hex,
                "purpose": "manual", "task_id": task_id, "event_id": None,
                "attempt_no": task.get("attempt_no", 1), "fingerprint": fingerprint(task),
                "action": action, "request": request, "paths": paths, "status": "pending",
                "actor_sub": actor_sub, "expires_at": time.time() + 600,
                "namelist_plan": validated["namelist_plan"] if validated else None,
                "validation_digest": validation_digest(validated) if validated else None,
                "changes": {key: {"before": task["request"].get(key), "after": value}
                            for key, value in request.items() if task["request"].get(key) != value},
                "recompute_completed":recompute,
                "impact": "将重新计算 WRF；旧尝试、远端输出及本地原始输出保留" if recompute else impact}
        self.store.save_recovery(plan)
        return plan

    def execute(self, task_id, plan_id, actor_sub=""):
        locker = getattr(self.manager, "_operation_lock", None)
        with locker(task_id) if locker else nullcontext():
            return self._execute(task_id, plan_id, actor_sub)

    def _execute(self, task_id, plan_id, actor_sub=""):
        plan = self.store.recovery(plan_id)
        if not plan or plan["task_id"] != task_id:
            raise ValueError("恢复计划不存在")
        if plan.get("actor_sub", "") != actor_sub:
            raise ValueError("恢复计划属于另一位操作员，请重新制定计划")
        if plan["status"] != "pending":
            return plan
        task = self.manager.get(task_id)
        if time.time() > plan["expires_at"] or fingerprint(task) != plan["fingerprint"]:
            raise ValueError("任务状态或恢复计划已变化，请重新诊断并确认")
        runtime = self.runtime_verifier()
        if plan.get("purpose", "repair") == "manual":
            self.manager.require_operation(task_id, plan["action"],**({"allow_completed":True} if plan.get("recompute_completed") else {}))
        else:
            diagnosis = self.diagnosis(task_id, plan["event_id"], runtime=runtime, force_refresh=True)
            if (not plan.get("diagnosis_digest") or diagnosis["diagnosis_digest"] != plan["diagnosis_digest"] or
                    diagnosis["capability_version"] != plan.get("capability_version")):
                raise ValueError("诊断证据或运行能力已变化，请重新制定并确认恢复计划")
            if plan["action"] not in diagnosis["actions"]:
                raise ValueError("当前证据不允许执行此恢复，请重新核查")
        if plan["action"] in {"restart", "reduce_time_step", "regenerate_plan"}:
            validated = validate_ordinary_request(plan["request"], runtime=runtime)
            if validation_digest(validated) != plan.get("validation_digest"):
                raise ValueError("运行配置校验已变化，请重新确认恢复计划")
            paths = self.manager.restart_plan(task_id,**({"allow_completed":True} if plan.get("purpose")=="manual" and plan.get("recompute_completed") else {}))
            if not paths["can_restart"] or paths["local_paths"] != plan["paths"]["local_paths"] or paths["remote_paths"] != plan["paths"]["remote_paths"]:
                raise ValueError("远端状态或清理路径已变化，请重新确认")
        if not self.store.claim_recovery(plan_id):
            current = self.store.recovery(plan_id)
            if current["status"] == "pending":
                raise ValueError("此事件已有恢复操作执行中或结果待核对，禁止重复执行")
            return current
        try:
            if plan["action"] == "resume":
                self.manager.resume_external(task_id)
            elif plan["action"] == "retry_outputs":
                self.manager.retry_outputs(task_id)
            elif plan["action"] == "render_partial":
                self.manager.render_partial(task_id)
            else:
                self.manager.restart(task_id, plan["request"], confirm_task_id=task_id, confirm_attempt=plan["attempt_no"],**({"allow_completed":True} if plan.get("purpose")=="manual" and plan.get("recompute_completed") else {}))
        except Exception:
            # The operation may already have reached the remote system. Never replay it.
            self.store.finish_recovery(plan_id, "unknown")
            raise
        self.store.finish_recovery(plan_id, "accepted")
        return self.store.recovery(plan_id)
