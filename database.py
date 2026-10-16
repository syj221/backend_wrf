from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_STATES = {
    "queued", "prefetching", "uploading", "running", "rendering", "cancel_pending", "reconciling"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TaskStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("CREATE TABLE IF NOT EXISTS wrf_incidents (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, data_json TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS wrf_recovery_plans (id TEXT PRIMARY KEY, data_json TEXT NOT NULL, status TEXT NOT NULL)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_diagnosis_snapshots (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                data_json TEXT NOT NULL)""")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS wrf_tasks (
                    id TEXT PRIMARY KEY,
                    owner_sub TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error TEXT,
                    failure_json TEXT,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_task_metadata (
                task_id TEXT PRIMARY KEY REFERENCES wrf_tasks(id) ON DELETE CASCADE,
                name TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_task_deletion_plans (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, actor_sub TEXT NOT NULL,
                status TEXT NOT NULL, phase TEXT NOT NULL, data_json TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_task_tombstones (
                task_id TEXT PRIMARY KEY, deletion_plan_id TEXT NOT NULL,
                status TEXT NOT NULL, deleted_at TEXT)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_forcing_policy (
                id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                enabled INTEGER NOT NULL, auto_cleanup INTEGER NOT NULL,
                retained_complete_cycles INTEGER NOT NULL,
                held_cycles_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_forcing_operations (
                id TEXT PRIMARY KEY, owner_sub TEXT NOT NULL, source TEXT NOT NULL,
                cycle TEXT NOT NULL, start_hour INTEGER NOT NULL, end_hour INTEGER NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}')""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_forcing_cleanup_audit (
                id TEXT PRIMARY KEY, actor_sub TEXT NOT NULL, source TEXT NOT NULL,
                paths_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}')""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_forcing_cleanup_plans (
                id TEXT PRIMARY KEY, actor_sub TEXT NOT NULL,
                status TEXT NOT NULL, data_json TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_forcing_retention_approvals (
                source TEXT PRIMARY KEY, contract TEXT NOT NULL, plan_id TEXT NOT NULL,
                approved_at TEXT NOT NULL)""")
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(wrf_tasks)").fetchall()}
            self._db.execute("""CREATE TABLE IF NOT EXISTS wrf_submissions (
                owner_sub TEXT NOT NULL, submission_key TEXT NOT NULL,
                request_json TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                accepted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(owner_sub, submission_key))""")
            if "owner_sub" not in columns:
                self._db.execute("ALTER TABLE wrf_tasks ADD COLUMN owner_sub TEXT NOT NULL DEFAULT ''")
            if "failure_json" not in columns:
                self._db.execute("ALTER TABLE wrf_tasks ADD COLUMN failure_json TEXT")
            if "attempt_no" not in columns:
                self._db.execute("ALTER TABLE wrf_tasks ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS wrf_task_attempts (
                    task_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    runtime_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    failure_json TEXT,
                    error TEXT,
                    log_path TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, attempt_no),
                    FOREIGN KEY (task_id) REFERENCES wrf_tasks(id) ON DELETE CASCADE
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_incident(self, task, evidence="", new_occurrence=False):
        from recovery import diagnose, digest
        diagnosis = diagnose(task, evidence)
        attempt = int(task.get("attempt_no") or 1)
        identifier = digest([task["id"], attempt, diagnosis["code"], task.get("stage")])[:32]
        now = utc_now()
        with self._lock, self._db:
            row = self._db.execute("SELECT data_json FROM wrf_incidents WHERE id=?", (identifier,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            if previous.get("status") in {"recovery_requested", "resolved"} and new_occurrence:
                identifier = digest([identifier, now])[:32]
                previous = {}
            incident = {**diagnosis, "id": identifier, "task_id": task["id"], "attempt_no": attempt,
                        "first_seen": previous.get("first_seen", now), "last_seen": now,
                        "status": previous.get("status", "open")}
            if not evidence:
                incident["evidence"] = previous.get("evidence", "")
            self._db.execute("INSERT OR REPLACE INTO wrf_incidents VALUES (?,?,?,?)", (identifier, task["id"], attempt, json.dumps(incident, ensure_ascii=False)))
        return incident

    def incidents(self, task_id):
        with self._lock:
            rows = self._db.execute("SELECT data_json FROM wrf_incidents WHERE task_id=? ORDER BY rowid DESC LIMIT 100", (task_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_diagnosis(self, diagnosis):
        """Persist the sanitized observation without rewriting incident identity."""
        fields = ("schema_version", "task_id", "event_id", "attempt_no", "code", "confidence",
                  "message", "facts", "unknowns", "evidence_signals", "evidence_sources",
                  "remote_status", "evidence_digest", "diagnosis_digest", "blockers", "actions",
                  "requires_confirmation", "capability_version", "observed_at")
        snapshot = {key: diagnosis[key] for key in fields if key in diagnosis}
        semantic = {key: value for key, value in snapshot.items()
                    if key not in {"observed_at", "evidence_sources"}}
        identifier = hashlib.sha256(json.dumps(semantic, ensure_ascii=False, sort_keys=True,
                                               allow_nan=False).encode()).hexdigest()
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO wrf_diagnosis_snapshots VALUES (?,?,?,?)",
                             (identifier, snapshot["task_id"], int(snapshot["attempt_no"]),
                              json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
        return identifier

    def diagnoses(self, task_id, attempt_no):
        """Return observations only after the caller checks task ownership."""
        with self._lock:
            rows = self._db.execute(
                "SELECT data_json FROM wrf_diagnosis_snapshots WHERE task_id=? AND attempt_no=? ORDER BY rowid DESC LIMIT 100",
                (task_id, attempt_no),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_recovery(self, plan):
        with self._lock, self._db:
            self._db.execute("INSERT INTO wrf_recovery_plans VALUES (?,?,?)", (plan["id"], json.dumps(plan, ensure_ascii=False), "pending"))

    def recovery(self, identifier):
        with self._lock:
            row = self._db.execute("SELECT data_json,status FROM wrf_recovery_plans WHERE id=?", (identifier,)).fetchone()
        return {**json.loads(row[0]), "status": row[1]} if row else None

    def claim_recovery(self, identifier):
        with self._lock, self._db:
            # Serialize the check and claim across independent service processes.
            self._db.execute("BEGIN IMMEDIATE")
            plan = self.recovery(identifier)
            if not plan or plan["status"] != "pending" or (plan.get("task_id") and self.deletion_blocked(plan["task_id"])):
                return False
            rows = self._db.execute("SELECT data_json FROM wrf_recovery_plans WHERE status IN ('executing','accepted','unknown')").fetchall()
            for row in rows:
                other = json.loads(row[0])
                same_event = bool(plan.get("event_id")) and other.get("event_id") == plan["event_id"]
                same_state = (bool(plan.get("fingerprint")) and other.get("task_id") == plan.get("task_id")
                              and other.get("attempt_no") == plan.get("attempt_no")
                              and other.get("fingerprint") == plan["fingerprint"])
                if same_event or same_state:
                    return False
            return self._db.execute("UPDATE wrf_recovery_plans SET status='executing' WHERE id=? AND status='pending'", (identifier,)).rowcount == 1

    def finish_recovery(self, identifier, status):
        with self._lock, self._db:
            self._db.execute("UPDATE wrf_recovery_plans SET status=? WHERE id=?", (status, identifier))
            plan = self.recovery(identifier)
            if status == "accepted" and plan.get("event_id"):
                row = self._db.execute("SELECT data_json FROM wrf_incidents WHERE id=?", (plan["event_id"],)).fetchone()
                if row:
                    event = json.loads(row[0]); event["status"] = "resolved" if self.get(plan["task_id"])["status"] == "succeeded" else "recovery_requested"
                    self._db.execute("UPDATE wrf_incidents SET data_json=? WHERE id=?", (json.dumps(event, ensure_ascii=False), plan["event_id"]))

    def reserve_submission(self, owner: str, key: str, request: dict, task_id: str) -> dict:
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO wrf_submissions VALUES (?,?,?,?,0)",
                             (owner, key, canonical, task_id))
            row = self._db.execute("SELECT * FROM wrf_submissions WHERE owner_sub=? AND submission_key=?",
                                   (owner, key)).fetchone()
            if row["request_json"] != canonical:
                raise ValueError("同一幂等键不能提交不同配置")
            return dict(row)

    def accept_submission(self, owner: str, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE wrf_submissions SET accepted=1 WHERE owner_sub=? AND submission_key=?", (owner, key))

    def get_submission(self, owner: str, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT task_id, accepted FROM wrf_submissions WHERE owner_sub=? AND submission_key=?",
                (owner, key),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        value["runtime"] = json.loads(value.pop("runtime_json") or "{}")
        result = value.pop("result_json")
        value["result"] = json.loads(result) if result else None
        failure = value.pop("failure_json", None)
        value["failure"] = json.loads(failure) if failure else None
        return value

    @staticmethod
    def _decode_attempt(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("request_json", "runtime_json", "result_json", "failure_json"):
            raw = value.pop(key, None)
            value[key.removesuffix("_json")] = json.loads(raw) if raw else None
        return value

    def create(self, task_id: str, request: dict[str, Any], owner_sub: str = "") -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO wrf_tasks (id,owner_sub,status,stage,progress,request_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, owner_sub, "queued", "queued", 0, json.dumps(request, ensure_ascii=False), now, now),
            )
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM wrf_tasks WHERE id=?", (task_id,)).fetchone()
        return self._decode(row)

    def list(self, limit: int = 50, owner_sub: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._lock:
            if owner_sub is None:
                rows = self._db.execute("SELECT * FROM wrf_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM wrf_tasks WHERE owner_sub=? ORDER BY created_at DESC LIMIT ?",
                    (owner_sub, limit),
                ).fetchall()
        return [self._decode(row) for row in rows]

    def metadata(self, task_id):
        with self._lock:
            row = self._db.execute("SELECT name,notes,archived,version,updated_at FROM wrf_task_metadata WHERE task_id=?", (task_id,)).fetchone()
        return ({**dict(row), "archived": bool(row["archived"])} if row else
                {"name": "", "notes": "", "archived": False, "version": 0, "updated_at": None})

    def patch_metadata(self, task_id, version, changes):
        if set(changes) - {"name", "notes", "archived"}:
            raise ValueError("不允许修改计算配置")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if not self.get(task_id):
                raise ValueError("任务不存在")
            current = self.metadata(task_id)
            if current["version"] != version:
                raise ValueError("任务说明已被其他操作修改，请刷新后重试")
            updated = {**current, **changes, "version": version + 1, "updated_at": utc_now()}
            self._db.execute("INSERT OR REPLACE INTO wrf_task_metadata VALUES (?,?,?,?,?,?)",
                (task_id, updated["name"], updated["notes"], int(updated["archived"]), updated["version"], updated["updated_at"]))
        return updated

    def page(self, limit=50, owner_sub=None, search="", source=None, status=None, archived=False, cursor=None):
        # Keyset pagination remains stable when new tasks arrive between pages.
        import base64
        filters, params = (["1=1"], []) if archived is None else (["COALESCE(m.archived,0)=?"], [int(archived)])
        if owner_sub is not None:
            filters.append("t.owner_sub=?"); params.append(owner_sub)
        if source:
            filters.append("json_extract(t.request_json,'$.data_source')=?"); params.append(source)
        if status:
            filters.append("t.status=?"); params.append(status)
        if search:
            filters.append("(instr(lower(t.id),lower(?))>0 OR instr(lower(COALESCE(m.name,'')),lower(?))>0 OR instr(lower(COALESCE(m.notes,'')),lower(?))>0)")
            params.extend([search]*3)
        where = " AND ".join(filters)
        join = " FROM wrf_tasks t LEFT JOIN wrf_task_metadata m ON m.task_id=t.id WHERE "
        with self._lock:
            total = self._db.execute("SELECT count(*)" + join + where, params).fetchone()[0]
            if cursor:
                try:
                    position = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                    if not isinstance(position,list) or len(position)!=2 or not all(isinstance(x,str) for x in position):
                        raise ValueError()
                except Exception:
                    raise ValueError("分页游标无效") from None
                where += " AND (t.created_at,t.id)<(?,?)"; params.extend(position)
            rows = self._db.execute("SELECT t.*" + join + where + " ORDER BY t.created_at DESC,t.id DESC LIMIT ?", [*params, limit+1]).fetchall()
            items = [{**self._decode(row), "metadata": self.metadata(row["id"])} for row in rows[:limit]]
        next_cursor = (base64.urlsafe_b64encode(json.dumps([items[-1]["created_at"],items[-1]["id"]]).encode()).decode() if len(rows)>limit else None)
        return {"items": items, "total": total, "next_cursor": next_cursor}

    def save_deletion(self, plan):
        with self._lock, self._db:
            self._db.execute("INSERT INTO wrf_task_deletion_plans VALUES (?,?,?,?,?,?)",
                             (plan["id"], plan["task_id"], plan["actor_sub"], plan["status"], plan["phase"], json.dumps(plan, ensure_ascii=False)))

    def deletion(self, plan_id):
        with self._lock:
            row = self._db.execute("SELECT data_json,status,phase FROM wrf_task_deletion_plans WHERE id=?", (plan_id,)).fetchone()
        return {**json.loads(row["data_json"]), "status": row["status"], "phase": row["phase"]} if row else None

    def update_deletion_detail(self, plan_id, **changes):
        with self._lock, self._db:
            plan = self.deletion(plan_id)
            if not plan:
                raise ValueError("删除操作不存在")
            plan.update(changes)
            self._db.execute("UPDATE wrf_task_deletion_plans SET data_json=?,status=?,phase=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), plan["status"], plan["phase"], plan_id))

    def reserve_deletion_execution(self, plan_id):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            plan = self.deletion(plan_id)
            if not plan or plan["status"] != "pending" or plan.get("execution_requested"):
                return False
            plan.update(execution_requested=True, phase="checking_execution")
            self._db.execute("UPDATE wrf_task_deletion_plans SET data_json=?,phase=? WHERE id=?",
                (json.dumps(plan,ensure_ascii=False),plan["phase"],plan_id))
            return True

    def deletion_blocked(self, task_id):
        with self._lock:
            return self._db.execute("SELECT 1 FROM wrf_task_tombstones WHERE task_id=?", (task_id,)).fetchone() is not None

    def claim_deletion(self, plan_id, task_id):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            plan = self.deletion(plan_id)
            task = self.get(task_id)
            if not plan or plan["status"] != "pending" or not task or task["status"] not in {"succeeded", "partial_success", "failed", "cancelled", "waiting_restart"}:
                return False
            if self.deletion_blocked(task_id):
                return False
            from retirement import sha
            observed = sha([task["status"],task["stage"],task["attempt_no"],task["request"],task["runtime"],task.get("result")])
            if observed != plan["manifest"]["task_fingerprint"]:
                return False
            for row in self._db.execute("SELECT data_json,status FROM wrf_recovery_plans WHERE status IN ('executing','accepted','unknown')"):
                if json.loads(row["data_json"]).get("task_id") == task_id and row["status"] in {"executing", "unknown"}:
                    return False
            self._db.execute("INSERT INTO wrf_task_tombstones VALUES (?,?,?,NULL)", (task_id,plan_id,"deleting"))
            self._db.execute("UPDATE wrf_task_deletion_plans SET status='executing',phase='claimed' WHERE id=?", (plan_id,))
            return True

    def advance_deletion(self, plan_id, phase, status="executing"):
        with self._lock, self._db:
            self._db.execute("UPDATE wrf_task_deletion_plans SET phase=?,status=? WHERE id=?", (phase,status,plan_id))

    def reserve_directory_reconciliation(self, plan_id, task_id, actor, proof):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            plan = self.deletion(plan_id)
            tombstone = self._db.execute("SELECT deletion_plan_id,status FROM wrf_task_tombstones WHERE task_id=?",(task_id,)).fetchone()
            if (not plan or plan["status"] != "unknown" or plan["task_id"] != task_id or plan["actor_sub"] != actor
                    or not tombstone or tombstone["deletion_plan_id"] != plan_id or tombstone["status"] != "deleting"):
                return False
            history = plan.get("reconciliations",[])
            history.append({**proof,"previous_started_at":plan.get("started_at"),"previous_error":plan.get("error")})
            plan.update(reconciliations=history,reconciliation_requested=True,error=None,started_at=None,
                        reconciliation_accepted_at=proof["verified_at"])
            plan.pop("finished_at",None)
            self._db.execute("UPDATE wrf_task_deletion_plans SET status='executing',phase='queued',data_json=? WHERE id=?",(json.dumps(plan,ensure_ascii=False),plan_id))
            return True

    def resume_deletion(self, plan_id):
        with self._lock, self._db:
            return self._db.execute("UPDATE wrf_task_deletion_plans SET status='executing' WHERE id=? AND status='unknown'", (plan_id,)).rowcount == 1

    def purge_deleted_task(self, task_id, plan_id, cleanup_pending=False):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT deletion_plan_id,status FROM wrf_task_tombstones WHERE task_id=?", (task_id,)).fetchone()
            if not row or row["deletion_plan_id"] != plan_id or row["status"] != "deleting":
                raise ValueError("任务删除状态不一致")
            self._db.execute("DELETE FROM wrf_diagnosis_snapshots WHERE task_id=?", (task_id,))
            self._db.execute("DELETE FROM wrf_incidents WHERE task_id=?", (task_id,))
            for record in self._db.execute("SELECT id,data_json FROM wrf_recovery_plans").fetchall():
                if json.loads(record["data_json"]).get("task_id") == task_id:
                    self._db.execute("DELETE FROM wrf_recovery_plans WHERE id=?", (record["id"],))
            self._db.execute("DELETE FROM wrf_tasks WHERE id=?", (task_id,))
            self._db.execute("UPDATE wrf_task_tombstones SET status='deleted',deleted_at=? WHERE task_id=?", (utc_now(),task_id))
            plan = self.deletion(plan_id)
            if cleanup_pending:
                plan.update(removed=True,removed_at=datetime.now(timezone.utc).timestamp(),cleanup_status="pending")
            else:
                plan.update(finished_at=datetime.now(timezone.utc).timestamp())
            self._db.execute("UPDATE wrf_task_deletion_plans SET status=?,phase=?,data_json=? WHERE id=?",
                ("executing" if cleanup_pending else "complete","space_cleanup" if cleanup_pending else "complete",json.dumps(plan,ensure_ascii=False),plan_id))
            # Accepted wrf_submissions remain as idempotent tombstones.

    def forcing_policy(self, default_enabled=False):
        with self._lock:
            row = self._db.execute("SELECT * FROM wrf_forcing_policy WHERE id=1").fetchone()
        if not row:
            return {"version":0,"enabled":bool(default_enabled),"auto_cleanup":False,
                    "retained_complete_cycles":2,"held_cycles":{"gfs":[],"ecmwf":[]},"updated_at":None}
        return {"version":row["version"],"enabled":bool(row["enabled"]),
                "auto_cleanup":bool(row["auto_cleanup"]),
                "retained_complete_cycles":row["retained_complete_cycles"],
                "held_cycles":json.loads(row["held_cycles_json"]),"updated_at":row["updated_at"]}

    def retention_approved(self, source):
        with self._lock:
            row = self._db.execute("SELECT contract FROM wrf_forcing_retention_approvals WHERE source=?",(source,)).fetchone()
        return bool(row and row[0] == "two-complete-terminal-release-1")

    def approve_retention(self, source, plan_id):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO wrf_forcing_retention_approvals VALUES (?,?,?,?)",
                (source,"two-complete-terminal-release-1",plan_id,utc_now()))

    def save_forcing_policy(self, version, enabled, auto_cleanup, held_cycles, default_enabled=False):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            before=self.forcing_policy(default_enabled)
            if before["version"] != version:
                raise ValueError("资料策略已被其他操作修改，请刷新")
            self._db.execute("INSERT OR REPLACE INTO wrf_forcing_policy VALUES (1,?,?,?,?,?,?)",
                (version+1,int(enabled),int(auto_cleanup),2,json.dumps(held_cycles,sort_keys=True),utc_now()))
        return self.forcing_policy(default_enabled)

    def record_forcing_operation(self, operation):
        with self._lock, self._db:
            self._db.execute("INSERT INTO wrf_forcing_operations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (operation["id"],operation["owner_sub"],operation["source"],operation["cycle"],
                 operation["start_hour"],operation["end_hour"],operation["status"],
                 operation["created_at"],operation["updated_at"],json.dumps(operation.get("detail") or {},ensure_ascii=False)))

    def update_forcing_operation(self, identifier, status, detail):
        with self._lock, self._db:
            before=self.forcing_operation(identifier)
            if not before:
                raise ValueError("下载操作不存在")
            self._db.execute("UPDATE wrf_forcing_operations SET status=?,updated_at=?,detail_json=? WHERE id=?",
                (status,utc_now(),json.dumps({**before["detail"],**detail},ensure_ascii=False),identifier))

    def list_forcing_operations(self, owner_sub=None, limit=50):
        with self._lock:
            query="SELECT id FROM wrf_forcing_operations"
            args=[]
            if owner_sub is not None:
                query+=" WHERE owner_sub=?"
                args.append(owner_sub)
            query+=" ORDER BY created_at DESC LIMIT ?"
            args.append(min(max(int(limit),1),100))
            identities=[row[0] for row in self._db.execute(query,args)]
        return [self.forcing_operation(identity) for identity in identities]

    def recover_interrupted_operations(self):
        """Run once before accepting requests; never resubmit an uncertain write."""
        with self._lock, self._db:
            stopped_at=datetime.now(timezone.utc).timestamp()
            self._db.execute("UPDATE wrf_forcing_cleanup_plans SET data_json=json_set(data_json,'$.finished_at',?) WHERE status IN ('checking','executing')",(stopped_at,))
            self._db.execute("UPDATE wrf_task_deletion_plans SET data_json=json_set(data_json,'$.finished_at',?) WHERE status IN ('checking','executing') OR (status='pending' AND json_extract(data_json,'$.execution_requested')=1)",(stopped_at,))
            self._db.execute("UPDATE wrf_forcing_operations SET status='unknown' WHERE status='submitting'")
            self._db.execute("UPDATE wrf_forcing_operations SET status='cancel_unknown' WHERE status='cancel_pending'")
            self._db.execute("UPDATE wrf_forcing_cleanup_plans SET status='unknown' WHERE status='executing'")
            self._db.execute("UPDATE wrf_forcing_cleanup_plans SET status='blocked',data_json=json_set(data_json,'$.phase','interrupted') WHERE status='checking' AND json_extract(data_json,'$.cycle') IS NOT NULL")
            self._db.execute("UPDATE wrf_task_deletion_plans SET status='unknown' WHERE status='executing'")
            self._db.execute("UPDATE wrf_task_deletion_plans SET status='blocked',phase='interrupted' WHERE status='checking' OR (status='pending' AND json_extract(data_json,'$.execution_requested')=1)")

    def forcing_download_reference_counts(self, source):
        with self._lock:
            rows = self._db.execute("SELECT cycle,COUNT(*) FROM wrf_forcing_operations WHERE source=? AND status IN ('submitting','accepted','unknown','cancel_pending','cancel_unknown') GROUP BY cycle",(source,)).fetchall()
        return {row[0]:row[1] for row in rows}

    def forcing_operation(self, identifier):
        with self._lock:
            row=self._db.execute("SELECT * FROM wrf_forcing_operations WHERE id=?",(identifier,)).fetchone()
        return ({"id":row["id"],"owner_sub":row["owner_sub"],"source":row["source"],
                 "cycle":row["cycle"],"start_hour":row["start_hour"],"end_hour":row["end_hour"],
                 "status":row["status"],"created_at":row["created_at"],"updated_at":row["updated_at"],
                 "detail":json.loads(row["detail_json"])} if row else None)

    def record_forcing_cleanup(self, identifier, actor_sub, source, paths, status, detail=None):
        with self._lock, self._db:
            self._db.execute("INSERT INTO wrf_forcing_cleanup_audit VALUES (?,?,?,?,?,?,?)",
                (identifier, actor_sub, source, json.dumps(paths, ensure_ascii=False),
                 status, utc_now(), json.dumps(detail or {}, ensure_ascii=False)))

    def forcing_download_cycles(self, source):
        with self._lock:
            return {row[0] for row in self._db.execute(
                "SELECT DISTINCT cycle FROM wrf_forcing_operations WHERE source=? AND status IN ('submitting','accepted','unknown','cancel_pending','cancel_unknown')",(source,))}

    def save_forcing_cleanup_plan(self, plan):
        with self._lock, self._db:
            self._db.execute("INSERT INTO wrf_forcing_cleanup_plans VALUES (?,?,?,?)",
                (plan["id"],plan["actor_sub"],plan["status"],json.dumps(plan,ensure_ascii=False)))

    def forcing_cleanup_plan(self, identifier):
        with self._lock:
            row=self._db.execute("SELECT status,data_json FROM wrf_forcing_cleanup_plans WHERE id=?",(identifier,)).fetchone()
        return {**json.loads(row["data_json"]),"status":row["status"]} if row else None

    def update_forcing_cleanup_plan(self, identifier, status, expected=None):
        with self._lock, self._db:
            query="UPDATE wrf_forcing_cleanup_plans SET status=? WHERE id=?"
            args=[status,identifier]
            if expected is not None:
                query+=" AND status=?"
                args.append(expected)
            return self._db.execute(query,args).rowcount == 1

    def update_forcing_cleanup_detail(self, identifier, **changes):
        with self._lock, self._db:
            plan = self.forcing_cleanup_plan(identifier)
            if not plan:
                raise ValueError("资料删除操作不存在")
            plan.update(changes)
            self._db.execute("UPDATE wrf_forcing_cleanup_plans SET status=?,data_json=? WHERE id=?",
                (plan["status"],json.dumps(plan,ensure_ascii=False),identifier))

    def update_forcing_cleanup(self, identifier, status, detail=None):
        with self._lock, self._db:
            self._db.execute("UPDATE wrf_forcing_cleanup_audit SET status=?,detail_json=? WHERE id=?",
                (status, json.dumps(detail or {}, ensure_ascii=False), identifier))

    def active(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM wrf_tasks WHERE status IN ({placeholders}) ORDER BY created_at", tuple(ACTIVE_STATES)
            ).fetchall()
        return [self._decode(row) for row in rows]

    def update(self, task_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"status", "stage", "progress", "request", "runtime", "result", "failure", "error", "attempt_no"}
        updates: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            column = {
                "request": "request_json",
                "runtime": "runtime_json",
                "result": "result_json",
                "failure": "failure_json",
            }.get(key, key)
            if key in {"request", "runtime", "result", "failure"}:
                value = json.dumps(value, ensure_ascii=False) if value is not None else None
            updates.append(f"{column}=?")
            params.append(value)
        updates.append("updated_at=?")
        params.append(utc_now())
        params.append(task_id)
        with self._lock, self._db:
            self._db.execute(f"UPDATE wrf_tasks SET {', '.join(updates)} WHERE id=?", params)
        task = self.get(task_id)
        if values.get("failure"):
            remote = values["failure"].get("remote") or {}
            self.record_incident(task, str(values["failure"].get("evidence") or remote.get("message") or ""), new_occurrence=True)
        if values.get("status") == "succeeded":
            with self._lock, self._db:
                for event in self.incidents(task_id):
                    if event["status"] in {"open", "recovery_requested"}:
                        event.update(status="resolved", resolved_at=utc_now())
                        self._db.execute("UPDATE wrf_incidents SET data_json=? WHERE id=?", (json.dumps(event, ensure_ascii=False), event["id"]))
        return task

    def archive_attempt(self, task_id: str, log_path: str | None = None) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        now = utc_now()
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT OR REPLACE INTO wrf_task_attempts (
                    task_id,attempt_no,request_json,status,stage,progress,runtime_json,
                    result_json,failure_json,error,log_path,started_at,finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    int(task.get("attempt_no") or 1),
                    json.dumps(task["request"], ensure_ascii=False),
                    task["status"],
                    task["stage"],
                    int(task.get("progress") or 0),
                    json.dumps(task.get("runtime") or {}, ensure_ascii=False),
                    json.dumps(task.get("result"), ensure_ascii=False) if task.get("result") is not None else None,
                    json.dumps(task.get("failure"), ensure_ascii=False) if task.get("failure") is not None else None,
                    task.get("error"),
                    log_path,
                    (task.get("runtime") or {}).get("attempt_started_at") or task.get("created_at") or now,
                    now,
                ),
            )
        return task

    def begin_attempt(self, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return self.update(
            task_id,
            attempt_no=int(task.get("attempt_no") or 1) + 1,
            request=request,
            status="queued",
            stage="queued",
            progress=0,
            runtime={"attempt_started_at": utc_now()},
            result=None,
            failure=None,
            error=None,
        )

    def attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM wrf_task_attempts WHERE task_id=? ORDER BY attempt_no DESC",
                (task_id,),
            ).fetchall()
        return [self._decode_attempt(row) for row in rows]

    def with_status(self, *statuses: str) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM wrf_tasks WHERE status IN ({placeholders}) ORDER BY updated_at",
                tuple(statuses),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def latest_success(self, owner_sub: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            query = (
                "SELECT * FROM wrf_tasks WHERE status IN ('succeeded','partial_success') "
                "AND result_json IS NOT NULL"
            )
            params: tuple[Any, ...] = ()
            if owner_sub is not None:
                query += " AND owner_sub=?"
                params = (owner_sub,)
            row = self._db.execute(query + " ORDER BY updated_at DESC LIMIT 1", params).fetchone()
        return self._decode(row)

    def delete(self, task_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM wrf_tasks WHERE id=?", (task_id,))
        return cursor.rowcount > 0
