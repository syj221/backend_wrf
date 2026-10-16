"""Operator-only, output-only validator rebinding for immutable WRF attempts."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import uuid

OVERRIDE_SCHEMA = "wrf-output-validation-override-1"
PLAN_SCHEMA = "wrf-output-validation-upgrade-plan-1"
TERMINAL = {"succeeded", "partial_success", "failed", "cancelled"}
SHA = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[0-9a-f]{32}")
TASK = re.compile(r"wrf_(?:(?:gfs_|ecmwf_)?[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}|agent_[0-9a-f]{32})")
RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_bound_task(database_path, task_id, *, config_path=None):
    """Read exact saved configuration; never regenerate a historical plan."""
    if not TASK.fullmatch(task_id):
        raise ValueError("Invalid ordinary task ID")
    database_path = Path(database_path).resolve(strict=True)
    with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM wrf_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise ValueError("Task does not exist")
    path = Path(config_path) if config_path is not None else database_path.parent / "runs" / task_id / "task.config.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Saved task.config.json is missing or is a symlink")
    raw = path.read_bytes()
    config = json.loads(raw)
    if config.get("task_id") != task_id:
        raise ValueError("Saved configuration belongs to another task")
    task = {"id": row["id"], "attempt_no": row["attempt_no"], "status": row["status"],
            "request": json.loads(row["request_json"]), "runtime": json.loads(row["runtime_json"] or "{}"),
            "config": config, "failure": json.loads(row["failure_json"] or "null"),
            "error": row["error"], "config_sha256": hashlib.sha256(raw).hexdigest(),
            "config_path": str(path.resolve()), "_request_json": row["request_json"],
            "_runtime_json": row["runtime_json"], "_failure_json": row["failure_json"]}
    task_binding(task)
    return task


def task_binding(task):
    runtime = task["runtime"]
    binding = {"task_id": task["id"], "attempt_no": task["attempt_no"],
               "launch_attempt": runtime.get("launch_attempt"),
               "compute_release_id": runtime.get("release_id"),
               "compute_release_fingerprint": runtime.get("release_fingerprint"),
               "request_digest": digest(task["request"]),
               "namelist_plan_digest": (task.get("config", {}).get("namelist_plan") or {}).get("digest")}
    if (not TASK.fullmatch(str(binding["task_id"])) or type(binding["attempt_no"]) is not int
            or binding["attempt_no"] < 1 or not TOKEN.fullmatch(str(binding["launch_attempt"]))
            or not RELEASE.fullmatch(str(binding["compute_release_id"]))
            or any(not SHA.fullmatch(str(binding[name])) for name in ("compute_release_fingerprint", "namelist_plan_digest"))):
        raise ValueError("Task has no complete immutable attempt/configuration binding")
    return binding


def verify_override(task, override):
    """Verify identity locally; remote byte/manifest checks remain mandatory."""
    if not isinstance(override, dict) or override.get("schema") != OVERRIDE_SCHEMA:
        raise ValueError("Invalid output validation override schema")
    for name, expected in task_binding(task).items():
        if override.get(name) != expected:
            raise ValueError("Output validation override binding mismatch: " + name)
    for name in ("remote_task_config_sha256", "remote_namelist_sha256", "output_inventory_digest",
                 "validation_release_fingerprint", "validation_script_sha256", "validation_schedule_sha256"):
        if not SHA.fullmatch(str(override.get(name))):
            raise ValueError("Invalid output validation override digest: " + name)
    if (not RELEASE.fullmatch(str(override.get("validation_release_id")))
            or not TOKEN.fullmatch(str(override.get("audit_id")))
            or not isinstance(override.get("bound_at"), str)):
        raise ValueError("Incomplete output validation override audit/release binding")
    try:
        bound = datetime.fromisoformat(override["bound_at"].replace("Z", "+00:00"))
        if bound.tzinfo is None:
            raise ValueError("Missing timezone")
    except ValueError as exc:
        raise ValueError("Invalid output validation override timestamp") from exc
    if task.get("config_sha256") != override["remote_task_config_sha256"]:
        raise ValueError("Output validation override configuration bytes changed")
    return override


# This probe opens existing files and runs only the target output validator.
# It does not launch WPS/WRF, update a process record, or write any remote file.
PROBE = r'''
import hashlib,json,os,runpy,stat,sys
from pathlib import Path
control,output,release,token=Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
def sha(p):
    before=p.stat();h=hashlib.sha256()
    with p.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):h.update(block)
    after=p.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):raise ValueError('File changed while reading: '+str(p))
    return h.hexdigest()
state=json.loads((control/'launches'/token/'state.json').read_text())
if state.get('attempt')!=token or state.get('status')!='succeeded' or state.get('exit_code')!=0 or state.get('cancelled'):raise ValueError('Remote attempt is not confirmed succeeded')
cancellation=control/'launches'/token/'cancellation.json'
if cancellation.exists():raise ValueError('Remote attempt has a cancellation record')
process=state.get('process') or {}
if not all(k in process for k in ('pid','start_ticks','boot_id')):raise ValueError('Remote terminal process identity missing')
live=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit():continue
    try:fields=(p/'stat').read_text().rsplit(')',1)[1].split()
    except FileNotFoundError:continue
    except PermissionError as exc:raise ValueError('Cannot prove terminal session membership: unreadable /proc/'+p.name+'/stat') from exc
    if fields[0]!='Z' and int(fields[3])==int(process['pid']):live.append(int(p.name))
if live:raise ValueError('Original managed session still has live processes')
config_path=control/'task.json';config=json.loads(config_path.read_text());namelist=output/'namelist.input'
before_config=sha(config_path);before_namelist=sha(namelist)
inventory=[]
for p in sorted(output.glob('wrfout_d*_*')):
    s=p.lstat()
    if not stat.S_ISREG(s.st_mode):raise ValueError('Output is not a regular file')
    inventory.append({'name':p.name,'bytes':s.st_size,'sha256':sha(p)})
manifest=json.loads((release/'manifest.json').read_text());files=manifest.get('files',{})
hashes={name:sha(release/'scripts'/name) for name in ('validate_outputs.py','output_schedule.py')}
if any(files.get('scripts/'+name)!=value for name,value in hashes.items()):raise ValueError('Target validator manifest hash mismatch')
sys.path.insert(0,str(release/'scripts'))
validator=runpy.run_path(str(release/'scripts/validate_outputs.py'),run_name='wrf_operator_readonly_validation')['validate']
report=validator(output,config)
if before_config!=sha(config_path) or before_namelist!=sha(namelist):raise ValueError('Input changed during validation')
after=[]
for p in sorted(output.glob('wrfout_d*_*')):
    s=p.lstat()
    if not stat.S_ISREG(s.st_mode):raise ValueError('Output type changed')
    after.append({'name':p.name,'bytes':s.st_size,'sha256':sha(p)})
if after!=inventory:raise ValueError('Native output changed during validation')
print(json.dumps({'state':{k:state.get(k) for k in ('status','attempt','exit_code','process')},'config_task_id':config.get('task_id'),'namelist_plan_digest':(config.get('namelist_plan') or {}).get('digest'),'remote_task_config_sha256':before_config,'remote_namelist_sha256':before_namelist,'output_inventory':inventory,'validation_release_id':manifest.get('release_id'),'validation_release_fingerprint':manifest.get('fingerprint'),'validation_script_sha256':hashes['validate_outputs.py'],'validation_schedule_sha256':hashes['output_schedule.py'],'report':report}))
'''


def observe_outputs(settings, client, task, validation_release_id, *, timeout=900):
    from runtime_release import require_ready
    if not RELEASE.fullmatch(validation_release_id):
        raise ValueError("Invalid validation release ID")
    target = replace(settings, hpc_runtime_release=validation_release_id)
    ready = require_ready(client, target, force=True)
    release = settings.hpc_release_root.rstrip("/") + "/releases/" + validation_release_id
    args = (client.task_dir(task["id"]), client.remote_output_dir(task["id"]), release, task["runtime"]["launch_attempt"])
    command = ". " + shlex.quote(release + "/runtime.env") + " >/dev/null 2>&1 && "
    command += " ".join(shlex.quote(v) for v in (settings.hpc_runtime_python, "-B", "-c", PROBE, *args))
    observed = json.loads(client.run(command, timeout=timeout))
    if (observed.get("validation_release_id") != validation_release_id
            or observed.get("validation_release_fingerprint") != ready.get("fingerprint")):
        raise ValueError("Validated release identity changed")
    return observed


def validate_bound_outputs(client, settings, task, *, observer=observe_outputs, timeout=900):
    """Revalidate a bound attempt without changing any local or remote state."""
    override = verify_override(task, task["runtime"].get("output_validation_override"))
    observed = observer(settings, client, task, override["validation_release_id"], timeout=timeout)
    for name in ("remote_task_config_sha256", "remote_namelist_sha256", "validation_release_id",
                 "validation_release_fingerprint", "validation_script_sha256", "validation_schedule_sha256"):
        if observed.get(name) != override[name]:
            raise ValueError("Bound output validation evidence changed: " + name)
    if digest(observed["output_inventory"]) != override["output_inventory_digest"]:
        raise ValueError("Bound native output inventory changed")
    state = observed["state"]
    if (state.get("attempt") != override["launch_attempt"] or state.get("status") != "succeeded"
            or state.get("exit_code") != 0 or state.get("process") != task["runtime"].get("remote_process")
            or observed.get("config_task_id") != task["id"]
            or observed.get("namelist_plan_digest") != override["namelist_plan_digest"]):
        raise ValueError("Bound terminal compute/configuration identity changed")
    if observed["report"].get("schema") != "wrf-output-validation-1":
        raise ValueError("Bound output validator response schema is invalid")
    return observed["report"]


def make_plan(settings, client, task_id, validation_release_id, *, observer=observe_outputs, timeout=900):
    task = load_bound_task(settings.database_path, task_id, config_path=settings.run_dir / task_id / "task.config.json")
    if task["status"] not in {"failed", "succeeded", "partial_success"}:
        raise ValueError("Only failed output tasks or terminal published tasks can be inspected")
    runtime = task["runtime"]
    if runtime.get("output_validation_override"):
        raise ValueError("An override already exists; inspect its audit instead of replaying")
    if not runtime.get("remote_wrf_succeeded") or runtime.get("remote_terminal_status") != "succeeded":
        raise ValueError("Local task has no confirmed successful compute attempt")
    if task["status"] == "failed" and (task.get("failure") or {}).get("failure_class") != "output":
        raise ValueError("Failed task is not classified as an output-only failure")
    observed = observer(settings, client, task, validation_release_id, timeout=timeout)
    state = observed["state"]
    if (state.get("status") != "succeeded" or state.get("exit_code") != 0
            or state.get("attempt") != runtime["launch_attempt"]
            or state.get("process") != runtime.get("remote_process")):
        raise ValueError("Remote terminal attempt identity differs from the saved attempt")
    binding = task_binding(task)
    if (observed.get("config_task_id") != task_id
            or observed.get("namelist_plan_digest") != binding["namelist_plan_digest"]
            or observed.get("remote_task_config_sha256") != task["config_sha256"]):
        raise ValueError("Remote and local frozen configurations differ")
    fields = ("remote_task_config_sha256", "remote_namelist_sha256", "validation_release_id",
              "validation_release_fingerprint", "validation_script_sha256", "validation_schedule_sha256")
    selected = {key: observed[key] for key in fields}
    selected["output_inventory_digest"] = digest(observed["output_inventory"])
    report = observed["report"]
    blockers = [] if report.get("schema") == "wrf-output-validation-1" and report.get("complete") is True else ["complete_output_revalidation_failed"]
    plan = {"schema": PLAN_SCHEMA, "task_binding": binding, "validation_target": selected,
            "eligible_action": "bind_validator" if task["status"] == "failed" else "readonly_complete_revalidation",
            "task_status": task["status"], "remote_terminal": state, "output_inventory": observed["output_inventory"],
            "validation_report": report, "blockers": blockers, "changes_compute_or_configuration": False,
            "starts_computation_or_recovery": False}
    plan["digest"] = digest(plan)
    return plan


def write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("Audit parent cannot be a symlink")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    return (path.stat().st_dev, path.stat().st_ino)


@contextmanager
def reserve_api_port(port):
    """Hold both loopback families for the entire offline transaction."""
    connections = []
    try:
        for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                connection = socket.socket(family, socket.SOCK_STREAM)
            except OSError as exc:
                if family == socket.AF_INET6 and exc.errno == errno.EAFNOSUPPORT:
                    continue
                raise
            connections.append(connection)
            # Closed APIs may leave TIME_WAIT sockets. Reuse closed state,
            # but never steal an existing listener or ignore a bind failure.
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                connection.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            try:
                connection.bind((address, port))
                # Linux permits multiple non-listening SO_REUSEADDR binds.
                # Listen without accepting requests to reserve the port until
                # this transaction closes every socket and releases its lock.
                connection.listen(1)
            except OSError as exc:
                raise RuntimeError("Owned API must exit gently; IPv4/IPv6 port reservation failed") from exc
        yield
    finally:
        for connection in connections:
            connection.close()


from terminal_evidence import WAITING_PROBE, verify_waiting_restart


def offline_guard(settings, client=None, *, port_reserved=False, waiting_verifier=verify_waiting_restart):
    if not port_reserved:
        with reserve_api_port(settings.port):
            return offline_guard(settings, client, port_reserved=True, waiting_verifier=waiting_verifier)
    with closing(sqlite3.connect(Path(settings.database_path).as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        rows = db.execute("SELECT id,status,runtime_json,failure_json FROM wrf_tasks").fetchall()
    proofs = []
    for task_id, status, raw_runtime, raw_failure in rows:
        runtime = json.loads(raw_runtime or "{}")
        if status in TERMINAL:
            if (runtime.get("remote_launch_attempted") is True
                    and runtime.get("remote_terminal_status") not in {"failed", "cancelled", "succeeded"}
                    and runtime.get("remote_wrf_succeeded") is not True):
                raise RuntimeError("Terminal local task still has an active or unknown remote launch")
            continue
        if status != "waiting_restart":
            raise RuntimeError("Every ordinary task must be terminal or independently verified idle before upgrade")
        task = {"id": task_id, "runtime": runtime, "failure": json.loads(raw_failure or "null")}
        proof = waiting_verifier(settings, client, task)
        if proof.get("task_id") != task_id or proof.get("terminal") is not True:
            raise RuntimeError("Waiting-restart proof is incomplete or belongs to another task")
        proofs.append(proof)
    return proofs


@contextmanager
def held_maintenance_lock(path, held_lock=None):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError("Global maintenance lock cannot be a symlink")
    owned = path.open("a") if held_lock is None else None
    lock = owned if owned is not None else held_lock
    try:
        actual, expected = os.fstat(lock.fileno()), path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("Inherited maintenance lock is not the exact global lock inode")
        # The inherited descriptor shares the caller's open-file description.
        # Re-locking it is safe; never unlock or close the caller's handle.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if owned is not None:
            owned.close()


def apply_plan(plan, confirmed_digest, settings, client, audit_path, *, planner=make_plan,
               guard=offline_guard, lock_path=None, held_lock=None, port_reservation=reserve_api_port, timeout=900):
    content = dict(plan)
    expected = content.pop("digest", None)
    if (plan.get("schema") != PLAN_SCHEMA or expected != digest(content) or confirmed_digest != expected
            or plan.get("eligible_action") != "bind_validator" or plan.get("blockers")):
        raise ValueError("Explicit confirmation must match an eligible exact output-only upgrade plan")
    lock_path = Path(lock_path) if lock_path is not None else Path(__file__).parent.parent / ".run/wrf-maintenance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with held_maintenance_lock(lock_path, held_lock), port_reservation(settings.port):
        idle_proofs = guard(settings, client=client, port_reserved=True)
        current = planner(settings, client, plan["task_binding"]["task_id"], plan["validation_target"]["validation_release_id"], timeout=timeout)
        if current != plan:
            raise RuntimeError("Upgrade plan, terminal attempt, native output or target release changed")
        task = load_bound_task(settings.database_path, plan["task_binding"]["task_id"],
                               config_path=settings.run_dir / plan["task_binding"]["task_id"] / "task.config.json")
        override = {"schema": OVERRIDE_SCHEMA, **plan["task_binding"], **plan["validation_target"],
                    "audit_id": uuid.uuid4().hex, "bound_at": datetime.now(timezone.utc).isoformat()}
        verify_override(task, override)
        audit = {"schema": "wrf-output-validation-upgrade-audit-1", "phase": "prepared",
                 "plan_digest": expected, "override": override, "validation_report": plan["validation_report"],
                 "output_inventory": plan["output_inventory"], "operator_uid": os.getuid(),
                 "verified_idle_waiting_tasks": idle_proofs,
                 "compute_configuration_and_attempt_unchanged": True, "recovery_started": False}
        audit_path = Path(audit_path)
        audit_identity = write_private_json(audit_path, audit)
        runtime = dict(task["runtime"])
        runtime["output_validation_override"] = override
        with closing(sqlite3.connect(settings.database_path, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("UPDATE wrf_tasks SET runtime_json=? WHERE id=? AND status='failed' AND attempt_no=? "
                                "AND request_json=? AND runtime_json=? AND failure_json IS ?",
                                (json.dumps(runtime, ensure_ascii=False), task["id"], task["attempt_no"],
                                 task["_request_json"], task["_runtime_json"], task["_failure_json"]))
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError("Task changed concurrently; prepared audit retained and no retry allowed")
            db.commit()
        # A prepared audit plus a persisted matching override is recoverable
        # evidence if writing the completion marker fails. Never reapply it.
        audit["phase"] = "bound"
        audit["bound_at"] = override["bound_at"]
        completion = audit_path.with_name(audit_path.name + "." + override["audit_id"] + ".complete")
        write_private_json(completion, audit)
        current_audit = audit_path.lstat()
        if audit_path.is_symlink() or (current_audit.st_dev, current_audit.st_ino) != audit_identity:
            raise RuntimeError("Audit was replaced; completed sidecar retained, inspect instead of replaying")
        os.replace(completion, audit_path)
        return {"status": "bound", "override": override, "audit_path": str(audit_path),
                "task_status_unchanged": "failed", "recovery_started": False}
