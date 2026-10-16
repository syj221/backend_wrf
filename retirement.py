"""Manifest-bound retirement of exclusively owned downscale assets.

Inventory is read-only. Application rechecks terminal evidence, references,
catalog identity and every file before revoking publication and removing data.
Shared forcing, scientific sources and toolchains are never deletion roots.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import urllib.error
import urllib.request
import uuid

SCHEMA = "wrf-downscale-retirement-1"
TERMINAL = {"succeeded", "failed", "cancelled"}
DS_ID = re.compile(r"^ds_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}$")
WRF_ID = re.compile(r"^wrf_(?:gfs|ecmwf|ec)_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}$")
DISPLAY_STATIC_ORIGIN = "http://127.0.0.1:8002"


class NoStaticRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Credentials are authorized only for the local display service.
        return None


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def scan_root(root,hash_files=True,deadline=None):
    """Do not follow a symlink at the root or anywhere below it."""
    def check_deadline():
        if deadline is not None:
            import time
            if time.monotonic()>=deadline:raise TimeoutError("Deletion execution deadline exceeded")
    check_deadline()
    path = os.path.abspath(root)
    # Configured storage aliases may be symlinks, but the deletion root is not followed.
    parent = os.path.realpath(os.path.dirname(path))
    path = os.path.join(parent,os.path.basename(path))
    entries = []
    def walk(current,relative):
        check_deadline()
        before = os.lstat(current)
        mode = before.st_mode
        item = {"relative":relative,"size":before.st_size,"mtime_ns":before.st_mtime_ns,
                "device":before.st_dev,"inode":before.st_ino}
        if stat.S_ISLNK(mode):
            item.update(kind="symlink",target=os.readlink(current))
            item["sha256"] = hashlib.sha256(os.fsencode(item["target"])).hexdigest()
        elif stat.S_ISREG(mode) and hash_files:
            digest = hashlib.sha256()
            descriptor = os.open(current,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(descriptor,"rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev,opened.st_ino) != (before.st_dev,before.st_ino):
                    raise RuntimeError("File identity changed during inventory")
                for block in iter(lambda:stream.read(8*1024*1024),b""):
                    check_deadline()
                    digest.update(block)
            item.update(kind="file",sha256=digest.hexdigest())
        elif stat.S_ISREG(mode):
            item.update(kind="file")
        elif stat.S_ISDIR(mode):
            item.update(kind="directory")
        else:
            raise RuntimeError("Special file is outside the retirement contract: "+current)
        after = os.lstat(current)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
            raise RuntimeError("Asset changed during inventory: "+current)
        entries.append(item)
        if item["kind"] == "directory":
            for name in sorted(os.listdir(current)):
                walk(os.path.join(current,name),name if relative == "." else relative+"/"+name)
    try:
        walk(path,".")
    except FileNotFoundError:
        if entries:
            raise RuntimeError("Asset disappeared during inventory")
        return {"path":path,"exists":False,"entries":[],"file_count":0,"symlink_count":0,"bytes":0}
    return {"path":path,"exists":True,"entries":entries,
            "file_count":sum(e["kind"] == "file" for e in entries),
            "symlink_count":sum(e["kind"] == "symlink" for e in entries),
            "bytes":sum(e["size"] for e in entries if e["kind"] == "file")}


def delete_root(snapshot,verify=True,make_writable=False,deadline=None):
    """Delete a verified tree using directory descriptors and O_NOFOLLOW."""
    if verify and scan_root(snapshot["path"],deadline=deadline) != snapshot:
        raise RuntimeError("File manifest changed before deletion")
    if not snapshot["exists"]:
        return
    path = snapshot["path"]
    expected_entries = {entry["relative"]:entry for entry in snapshot["entries"]}
    # Walk the canonical ancestor chain without following newly inserted links.
    descriptor = os.open("/",os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in Path(path).parent.parts[1:]:
            following = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        def remove(parent_fd,name,relative):
            if deadline is not None:
                import time
                if time.monotonic()>=deadline:raise TimeoutError("Deletion execution deadline exceeded")
            before = os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
            expected = expected_entries[relative]
            if (before.st_dev,before.st_ino) != (expected["device"],expected["inode"]):
                raise RuntimeError("Asset identity changed during deletion")
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent_fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev,opened.st_ino) != (before.st_dev,before.st_ino):
                        raise RuntimeError("Directory identity changed before deletion")
                    if make_writable:
                        # Only the already verified, exclusively owned deletion
                        # directory is unsealed; symlink targets are never opened.
                        os.fchmod(child,stat.S_IMODE(before.st_mode)|stat.S_IWUSR|stat.S_IXUSR)
                    for item in sorted(os.listdir(child)):
                        remove(child,item,item if relative == "." else relative+"/"+item)
                finally:
                    os.close(child)
                current = os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
                if (current.st_dev,current.st_ino) != (before.st_dev,before.st_ino):
                    raise RuntimeError("Directory identity changed before removal")
                os.rmdir(name,dir_fd=parent_fd)
            else:
                current = os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
                if (current.st_dev,current.st_ino) != (before.st_dev,before.st_ino):
                    raise RuntimeError("Asset identity changed before removal")
                os.unlink(name,dir_fd=parent_fd)
        remove(descriptor,os.path.basename(path),".")
    finally:
        os.close(descriptor)


def remote_terminal(control,attempt,expected_status):
    """Read persisted state and independently check the dedicated session."""
    if attempt:
        if not re.fullmatch(r"[a-f0-9]{32}",attempt):
            raise RuntimeError("Invalid launch attempt")
        state_path = os.path.join(control,"launches",attempt,"state.json")
        with open(state_path) as stream:
            state = json.load(stream)
        status = "cancelled" if state.get("cancelled") and state.get("cancellation_verified") else state.get("status")
        if status != expected_status or status not in {"succeeded","failed","cancelled"}:
            raise RuntimeError("Remote terminal state is unverified")
        process = state.get("process") or {}
        if not process and state.get("reason") != "cancelled_before_launch":
            raise RuntimeError("Remote process identity is missing")
        pid = process.get("pid")
        if pid:
            if type(pid) is not int or pid <= 1 or not process.get("start_ticks") or not process.get("boot_id"):
                raise RuntimeError("Remote process identity is incomplete")
            with open("/proc/sys/kernel/random/boot_id") as stream:
                boot = stream.read().strip()
            if boot == process["boot_id"]:
                for name in os.listdir("/proc"):
                    if not name.isdigit():
                        continue
                    try:
                        with open("/proc/"+name+"/stat") as stream:
                            fields = stream.read().rsplit(")",1)[1].split()
                    except FileNotFoundError:
                        continue
                    if fields[0] != "Z" and (int(fields[3]) == pid or int(name) == pid and fields[19] == str(process["start_ticks"])):
                        raise RuntimeError("Remote launch session is still active")
        return {"status":status,"attempt":attempt,"process":process,
                "cancellation_verified":bool(state.get("cancellation_verified")),"verified":True}
    # Legacy ordinary jobs have no generation identity. A live/reused PID blocks deletion.
    with open(os.path.join(control,"exit.code")) as stream:
        exit_code = int(stream.read().strip())
    status = "succeeded" if exit_code == 0 else "failed"
    if status != expected_status:
        raise RuntimeError("Legacy exit evidence does not match task status")
    pid_path = os.path.join(control,"service.pid")
    pid = None
    if os.path.isfile(pid_path):
        with open(pid_path) as stream:
            pid = int(stream.read().strip())
        if os.path.exists("/proc/"+str(pid)):
            raise RuntimeError("Legacy PID remains allocated; terminal identity is uncertain")
    return {"status":status,"exit_code":exit_code,"verified":True,"legacy":True,
            "legacy_pid":pid}


def remote_session_absent(proof):
    """Recheck process identity after its control directory has been removed."""
    if not proof.get("verified"):
        raise RuntimeError("Missing approved terminal identity")
    process = proof.get("process") or {}
    pid = process.get("pid") or proof.get("legacy_pid")
    if not pid:
        if proof.get("cancellation_verified") or proof.get("never_started"):
            return {"verified":True}
        raise RuntimeError("Cannot reconcile without a retained process identity")
    if type(pid) is not int or pid <= 1:
        raise RuntimeError("Invalid retained process identity")
    with open("/proc/sys/kernel/random/boot_id") as stream:
        boot = stream.read().strip()
    if process and boot != process.get("boot_id"):
        return {"verified":True}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/"+name+"/stat") as stream:
                fields = stream.read().rsplit(")",1)[1].split()
        except FileNotFoundError:
            continue
        if fields[0] != "Z" and (int(fields[3]) == pid or int(name) == pid):
            raise RuntimeError("Retained launch session or PID is still allocated")
    return {"verified":True}


def discover_task_paths(root,identities):
    """Find every historical attempt without accepting a task-ID prefix match."""
    for identity in identities:
        if not re.fullmatch(r"wrf_(?:gfs|ecmwf|ec)_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}",identity):
            raise ValueError("Invalid ordinary task identity")
    result = []
    for name in sorted(os.listdir(root)):
        if any(re.fullmatch(r"(?:WRF_|WPS_|logs_|stage_status_)"+re.escape(identity)+r"(?:_attempt-[0-9]+-(?:cpu|gpu))?(?:\.jsonl)?",name) for identity in identities):
            result.append(os.path.join(root,name))
    return result


def environment_occupancy(roots):
    """Read live process paths; exclude this audit's own SSH ancestor chain."""
    real = [os.path.realpath(root) for root in roots]
    ancestors = set()
    ancestor = os.getpid()
    while ancestor > 1 and ancestor not in ancestors:
        ancestors.add(ancestor)
        try:
            with open("/proc/"+str(ancestor)+"/stat") as stream:
                ancestor = int(stream.read().rsplit(")",1)[1].split()[1])
        except OSError:
            break
    matches,unreadable = [],[]
    with open("/proc/sys/kernel/random/boot_id") as stream:
        boot_id = stream.read().strip()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in ancestors:
            continue
        base = "/proc/"+name
        try:
            if os.stat(base).st_uid != os.getuid():
                continue
            identity = {"pid":int(name),"boot_id":boot_id}
            with open(base+"/stat") as stream:
                fields = stream.read().rsplit(")",1)[1].split()
                identity["start_ticks"] = int(fields[19])
            with open(base+"/comm") as stream:
                identity["comm"] = stream.read().strip()
            with open(base+"/cmdline","rb") as stream:
                raw_command = stream.read()
                command = raw_command.replace(b"\x00",b" ").decode(errors="replace")
                identity["argv0_sha256"] = hashlib.sha256(raw_command.split(b"\x00",1)[0]).hexdigest()
            links,errors = [],[]
            for suffix in ("exe","cwd"):
                try:
                    links.append(os.readlink(base+"/"+suffix))
                except FileNotFoundError:
                    pass
                except OSError:
                    errors.append(suffix)
            try:
                for descriptor in os.listdir(base+"/fd"):
                    try:
                        links.append(os.readlink(base+"/fd/"+descriptor))
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass
            except OSError:
                errors.append("fd")
            try:
                with open(base+"/maps") as stream:
                    for line in stream:
                        fields = line.rstrip().split(None,5)
                        if len(fields) == 6 and fields[5].startswith("/"):
                            links.append(fields[5])
            except FileNotFoundError:
                pass
            except OSError:
                errors.append("maps")
            hits = [root for root in real if root in command or any(path == root or path.startswith(root+"/") for path in links)]
            if hits:
                matches.append({"pid":int(name),"matched_roots":hits})
            if errors:
                unreadable.append({**identity,"unreadable_surfaces":sorted(errors)})
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append({"pid":int(name),"unreadable_surfaces":["identity"]})
    return {"matches":sorted(matches,key=lambda x:x["pid"]),"unreadable_owned_processes":sorted(unreadable,key=lambda x:x["pid"])}


_EMBEDDED_FS_FUNCTIONS = "\n\n".join(inspect.getsource(f) for f in (scan_root,delete_root,remote_terminal,remote_session_absent,discover_task_paths,environment_occupancy))


def remote_call(client,operation,payload,timeout=3600):
    # The embedded program only imports the standard library on Python >=3.6.
    program = "import os,stat,hashlib,json,re,sys\nfrom pathlib import Path\n"
    program += _EMBEDDED_FS_FUNCTIONS
    program += "\np=json.loads(sys.argv[2])\nop=sys.argv[1]\n"
    program += "if op=='scan': r=[scan_root(x) for x in p]\n"
    program += "elif op=='paths': r=[os.path.join(os.path.realpath(os.path.dirname(os.path.abspath(x))),os.path.basename(os.path.abspath(x))) for x in p]\n"
    program += "elif op=='metadata':\n r=[]\n for x in p:\n  snapshot=scan_root(x,hash_files=False)\n  digest=hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n  r.append({'path':snapshot['path'],'metadata_sha256':digest})\n"
    program += "elif op=='deletion-metadata':\n r=[]\n for x in p:\n  snapshot=scan_root(x,hash_files=False)\n  r.append({'path':snapshot['path'],'bytes':snapshot['bytes'],'file_count':snapshot['file_count']})\n"
    program += "elif op=='discover': r=discover_task_paths(p['root'],p['identities'])\n"
    program += "elif op=='occupancy': r=environment_occupancy(p)\n"
    program += "elif op=='terminal': r=[remote_terminal(x['control'],x.get('attempt'),x['status']) for x in p]\n"
    program += "elif op=='session_absent': r=[remote_session_absent(x) for x in p]\n"
    program += "elif op=='delete':\n for x in p:\n  snapshot=scan_root(x['path'])\n  digest=hashlib.sha256(json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n  if digest!=x['snapshot_sha256']: raise RuntimeError('Approved remote snapshot changed')\n  delete_root(snapshot,verify=False,make_writable=x.get('make_writable') is True)\n r={'deleted':len(p)}\n"
    program += "elif op=='environment':\n r=[]\n for x in p:\n  item={'setting':x['setting'],'path':x['path'],'action':'retain_pending_ownership_review'}\n  item['real_path']=os.path.realpath(x['path'])\n  item['exists']=os.path.lexists(x['path'])\n  if item['exists']:\n   info=os.lstat(x['path']);item['size']=info.st_size;item['symlink']=stat.S_ISLNK(info.st_mode);item['directory']=os.path.isdir(x['path'])\n   if item['symlink']: item['target']=os.readlink(x['path'])\n   if stat.S_ISREG(info.st_mode): item['sha256']=scan_root(x['path'])['entries'][0]['sha256']\n  r.append(item)\n"
    program += "else: raise ValueError('Unknown operation')\nprint(json.dumps(r))\n"
    command = "python3 -B -c "+shlex.quote(program)+" "+shlex.quote(operation)+" "+shlex.quote(json.dumps(payload))
    if hasattr(type(client),"remote"):
        return json.loads(client.remote(command,operation,payload,timeout))
    return json.loads(client.run(command,timeout=timeout))


def read_tasks(path,table):
    with closing(sqlite3.connect(Path(path).as_uri()+"?mode=ro",uri=True,timeout=5)) as db:
        db.row_factory = sqlite3.Row
        return [{**dict(row),"request":json.loads(row["request_json"]),"runtime":json.loads(row["runtime_json"])}
                for row in db.execute("SELECT * FROM "+table)]


def references(value,target):
    if isinstance(value,dict):
        return any(references(v,target) for v in value.values())
    if isinstance(value,list):
        return any(references(v,target) for v in value)
    return value == target


def exclusive_tasks(downscale,ordinary,selected):
    selected = set(selected)
    if not selected or any(not DS_ID.fullmatch(t) for t in selected):
        raise ValueError("Explicit valid downscale task IDs are required")
    chosen = [t for t in downscale if t["id"] in selected]
    if len(chosen) != len(selected):
        raise ValueError("A selected downscale task does not exist")
    upstream,retained = [],[]
    for task in chosen:
        source = task["runtime"].get("source_wrf") or {}
        identity = source.get("task_id")
        if not identity:
            continue
        match = next((t for t in ordinary if t["id"] == identity),None)
        other_refs = [t["id"] for t in downscale if t["id"] not in selected and references(t,identity)]
        other_refs += [t["id"] for t in ordinary if t["id"] != identity and references(t,identity)]
        if (source.get("auto_created") is True and match and WRF_ID.fullmatch(identity)
                and match["request"].get("highres_parent_task_id") in selected and not other_refs):
            if identity not in {t["id"] for t in upstream}:
                upstream.append(match)
        else:
            retained.append({"task_id":identity,"reason":"shared_or_unverified_ownership","references":other_refs})
    return chosen,upstream,retained


def database_records(path,ids,kind,*,delete=False,expected=None):
    """Fingerprint only attributable records; never initialize or migrate a DB."""
    mode = "rw" if delete else "ro"
    with closing(sqlite3.connect(Path(path).as_uri()+"?mode="+mode,uri=True,timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if delete:
            db.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        family = (["wrf_submissions","wrf_task_attempts","wrf_incidents","wrf_diagnosis_snapshots","wrf_recovery_plans","wrf_tasks"]
                  if kind == "ordinary" else ["downscale_submissions","downscale_logs","downscale_certification_requests","downscale_certification_profiles","downscale_confirmations","mesoscale_forcing_artifacts","downscale_tasks"])
        picked,summaries = {},[]
        tasks_table = "wrf_tasks" if kind == "ordinary" else "downscale_tasks"
        tasks = [dict(r) for r in db.execute("SELECT * FROM "+tasks_table) if r["id"] in ids]
        for table in family:
            if table not in tables:
                continue
            rows = []
            for row in db.execute("SELECT rowid AS retirement_rowid,* FROM "+table):
                item = dict(row)
                owned = item.get("task_id") in ids or table == tasks_table and item.get("id") in ids
                if table == "wrf_recovery_plans":
                    owned = any(references(json.loads(item["data_json"]),identity) for identity in ids)
                if table == "downscale_confirmations":
                    owned = any(item.get("owner_sub") == t.get("owner_sub") and json.loads(item["request_json"]) == json.loads(t["request_json"]) for t in tasks)
                if table == "mesoscale_forcing_artifacts":
                    value = json.loads(item["manifest_json"])
                    owned = value.get("task_id") in ids or value.get("source_task_id") in ids
                if owned:
                    rows.append(item)
            picked[table] = rows
            summaries.append({"table":table,"count":len(rows),"sha256":sha(sorted(rows,key=lambda x:x["retirement_rowid"]))})
        if delete:
            if summaries != expected:
                raise RuntimeError("Task records changed before retirement")
            for table in family:
                for row in picked.get(table,[]):
                    db.execute("DELETE FROM "+table+" WHERE rowid=?",(row["retirement_rowid"],))
            db.commit()
        return summaries


def shared_forcing_references(path,owned_ids,tasks):
    """A retained task's artifact reference protects the source task's files."""
    result = []
    with closing(sqlite3.connect(Path(path).as_uri()+"?mode=ro",uri=True,timeout=5)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mesoscale_forcing_artifacts'").fetchone()
        if not exists:
            return result
        for row in db.execute("SELECT artifact_id,manifest_json FROM mesoscale_forcing_artifacts"):
            manifest = json.loads(row["manifest_json"])
            if manifest.get("task_id") not in owned_ids and manifest.get("source_task_id") not in owned_ids:
                continue
            retained = [t["id"] for t in tasks if t["id"] not in owned_ids and references(t,row["artifact_id"])]
            if retained:
                result.append({"artifact_id":row["artifact_id"],"referencing_tasks":sorted(retained)})
    return result


def catalog_snapshot(engine,task_ids,*,revoke=False,purge=False,expected=None):
    """Identity checking and catalog revocation share one SQL transaction."""
    from sqlalchemy import text
    results = []
    with engine.begin() as db:
        for task_id in sorted(task_ids):
            file_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL,"wrf-workbench:"+task_id))
            row = db.execute(text("SELECT file_uuid,data_type,source_path,meta_path,is_deleted,file_hash FROM public_info WHERE file_uuid=:id"),{"id":file_uuid}).mappings().first()
            assets = [dict(r) for r in db.execute(text("SELECT asset_uuid,file_uuid,dataset_id,parsed_data_path,webp_url,asset_status FROM wrf_info WHERE file_uuid=:id ORDER BY asset_uuid"),{"id":file_uuid}).mappings()]
            if row and (row["data_type"] != "WRF" or row["meta_path"] != "WRF/workbench/"+task_id+"/scene.meta.json" or any(r["dataset_id"] != task_id for r in assets)):
                raise RuntimeError("Catalog identity does not match the retirement task")
            refs = [str(r[0]) for r in db.execute(text("SELECT file_uuid FROM public_info WHERE source_file_uuid=:id ORDER BY file_uuid"),{"id":file_uuid})]
            refs += [str(r[0]) for r in db.execute(text("SELECT file_uuid FROM wrf_info WHERE parsed_data_path LIKE :prefix AND file_uuid!=:id ORDER BY file_uuid"),{"id":file_uuid,"prefix":"WRF/workbench/"+task_id+"/%"})]
            results.append({"task_id":task_id,"file_uuid":file_uuid,"record":dict(row) if row else None,"assets":assets,"references":refs})
        if revoke or purge:
            approved = json.loads(json.dumps(expected))
            if purge:
                for item in approved:
                    if item["record"]:
                        item["record"]["is_deleted"] = 1
                    for asset in item["assets"]:
                        asset["asset_status"] = "retired"
            if results != approved or any(r["references"] for r in results):
                raise RuntimeError("Catalog changed or has shared references")
            for item in results:
                if purge:
                    db.execute(text("DELETE FROM wrf_info WHERE file_uuid=:id"),{"id":item["file_uuid"]})
                    db.execute(text("DELETE FROM public_info WHERE file_uuid=:id"),{"id":item["file_uuid"]})
                else:
                    db.execute(text("UPDATE wrf_info SET asset_status='retired' WHERE file_uuid=:id"),{"id":item["file_uuid"]})
                    db.execute(text("UPDATE public_info SET is_deleted=1,delete_time=:now,delete_reason=:reason WHERE file_uuid=:id"),
                               {"id":item["file_uuid"],"now":datetime.now(timezone.utc).replace(tzinfo=None),"reason":"Approved high-resolution retirement"})
    return results


def observe_static_access(catalog,token=None):
    """Use authenticated HEAD requests without storing or printing credentials."""
    paths = set()
    for item in catalog:
        if item["record"]:
            paths.add("/data/WRF/workbench/"+item["task_id"]+"/scene.meta.json")
        for asset in item["assets"]:
            path = str(asset.get("webp_url") or "")
            if not path.startswith("/data/WRF/workbench/"+item["task_id"]+"/"):
                raise RuntimeError("Catalog asset URL is outside its task boundary")
            paths.add(path)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoStaticRedirect())
    result = []
    for path in sorted(paths):
        headers = {"Authorization":"Bearer "+token} if token else {}
        request = urllib.request.Request(DISPLAY_STATIC_ORIGIN+path,headers=headers,method="HEAD")
        try:
            with opener.open(request,timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
        except (OSError,ValueError):
            status = "unavailable"
        result.append({"path":path,"status":status,"authenticated":bool(token)})
    return result



LEGACY_ENVIRONMENT_SETTINGS = (
    "hpc_downscale_interface_root", "hpc_downscale_cfd_template",
    "hpc_downscale_openfoam_activate", "hpc_downscale_python",
)


class LegacyRetirementSettings:
    """Operator-only path bindings; ordinary service settings stay independent."""
    def __init__(self, base, paths):
        self._base = base
        self._paths = paths

    def __getattr__(self, name):
        if name in self._paths:
            return self._paths[name]
        return getattr(self._base, name)


def bind_legacy_settings(settings, manifest):
    """Reuse only paths from an explicitly selected, intact old inventory."""
    if (manifest.get("schema") != SCHEMA or manifest.get("digest") != manifest_digest(manifest)):
        raise ValueError("Legacy manifest schema or digest differs")
    paths = {}
    for item in manifest.get("environment_ownership_review", []):
        name, path = item.get("setting"), item.get("path")
        if name not in LEGACY_ENVIRONMENT_SETTINGS or name in paths:
            raise ValueError("Unexpected or duplicate legacy environment setting")
        if not isinstance(path, str) or not path.startswith("/") or ".." in Path(path).parts or re.search(r"[\s;|&<>`$(){}\[\]\\\"']", path):
            raise ValueError("Legacy environment path is not absolute and safe")
        paths[name] = path
    if paths and set(paths) != set(LEGACY_ENVIRONMENT_SETTINGS):
        raise ValueError("Legacy environment binding is incomplete")
    if manifest.get("dedicated_environment_ownership") and not paths:
        raise ValueError("Dedicated environment requires an explicit legacy manifest binding")
    return LegacyRetirementSettings(settings, paths)


def legacy_task_paths(settings, task_id):
    """Derive the former dedicated paths without any executable downscale API."""
    if not DS_ID.fullmatch(task_id):
        raise ValueError("Invalid retired downscale task ID")
    roots = [str(settings.hpc_remote_dir).rstrip("/"), str(settings.hpc_storage_alias).rstrip("/")]
    if any(not root.startswith("/") or ".." in Path(root).parts or re.search(r"[\s;|&<>`$(){}\[\]\\\"']", root) for root in roots):
        raise ValueError("Invalid legacy retirement root")
    return (roots[0]+"/backend_wrf_downscale_tasks/"+task_id,
            roots[1]+"/WRFwork/DOWNSCALE_"+task_id+"/run")


def dedicated_environment_roots(settings,ownership):
    """Accept only the audited, narrowly defined downscale environment roots."""
    if not ownership:
        return []
    if ownership.get("schema") != "wrf-downscale-environment-ownership-1":
        raise ValueError("Unsupported dedicated environment ownership evidence")
    cfd = Path(settings.hpc_downscale_openfoam_activate).parent
    python = Path(settings.hpc_downscale_python)
    environment = python.parent.parent
    if python.parent.name != "bin" or environment.name != "wrfpost" or cfd.name != "CFD":
        raise ValueError("Configured environment layout requires separate ownership review")
    allowed = {str(Path(settings.hpc_downscale_interface_root)),str(Path(settings.hpc_downscale_cfd_template)),
               str(Path(settings.hpc_downscale_openfoam_activate)),str(environment),str(environment.parent/"foamgeo"),
               str(environment.parent/"openfoam7"),str(cfd/"run"),str(cfd/"user-7")}
    if (Path(settings.hpc_downscale_interface_root).parent != cfd
            or Path(settings.hpc_downscale_cfd_template).parent.parent != cfd):
        raise ValueError("CFD tools and template must belong to the audited dedicated root")
    roots = ownership.get("dedicated_roots")
    evidence = ownership.get("evidence") or {}
    classifications = ownership.get("classification") or []
    classified = {item.get("path") for item in classifications if isinstance(item,dict) and item.get("dedicated") is True}
    inspected = {item.get("path") for item in evidence.get("roots",[]) if isinstance(item,dict)}
    if (not isinstance(roots,list) or not roots or any(not isinstance(p,str) or p not in allowed for p in roots)
            or len(set(roots)) != len(roots) or evidence.get("processes") or (evidence.get("lsof") or {}).get("matches")
            or evidence.get("scan_errors") or (evidence.get("lsof") or {}).get("returncode") != 0
            or not set(roots).issubset(classified & inspected)):
        raise ValueError("Environment ownership evidence is incomplete or has unsafe references")
    for item in ownership.get("protected_process_exceptions",[]):
        if (item.get("comm") not in {"(sd-pam)","sshd","gnome-keyring-d","sftp-server","ssh-agent"}
                or type(item.get("pid")) is not int or item["pid"] <= 1
                or type(item.get("start_ticks")) is not int or item["start_ticks"] <= 0
                or not re.fullmatch(r"[a-f0-9-]{36}",item.get("boot_id",""))
                or not re.fullmatch(r"[a-f0-9]{64}",item.get("argv0_sha256",""))):
            raise ValueError("Protected process exception requires an exact audited system process identity")
    return sorted(roots)


def environment_occupancy_blocked(occupancy,ownership):
    """Never generalize a reviewed exception to another PID or generation."""
    if (not isinstance(occupancy.get("matches"),list)
            or not isinstance(occupancy.get("unreadable_owned_processes"),list) or occupancy["matches"]):
        return True
    approved = (ownership or {}).get("protected_process_exceptions",[])
    keys = ("pid","start_ticks","boot_id","argv0_sha256","comm")
    for process in occupancy.get("unreadable_owned_processes",[]):
        if not any(all(process.get(key) == item.get(key) and process.get(key) is not None for key in keys) for item in approved):
            return True
    return False


def legacy_retirement_assets(settings):
    """Bind an explicit migration map without trusting it as ownership evidence."""
    path = getattr(settings,"legacy_asset_manifest",None)
    if path is None:
        return None
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 16*1024*1024:
        raise ValueError("Legacy asset mapping is missing or unsafe")
    raw = path.read_bytes()
    value = json.loads(raw)
    current = Path(settings.database_path).resolve()
    if value.get("schema") != "wrf-local-assets-1" or Path(value["target_database"]).resolve() != current:
        raise ValueError("Legacy asset mapping belongs to another active database")
    mappings = value.get("database_mappings")
    if not isinstance(mappings,list) or not isinstance(value.get("tasks"),dict):
        raise ValueError("Legacy database mappings are incomplete")
    checked = []
    for item in mappings:
        kind = item.get("kind")
        source,target = Path(item["source"]),Path(item["target"])
        if (kind not in {"ordinary","downscale"} or not source.is_absolute() or not target.is_absolute()
                or source.is_symlink() or target.is_symlink() or not source.is_file() or not target.is_file()):
            raise ValueError("Legacy database path is unsafe")
        source,target = source.resolve(),target.resolve()
        if kind == "ordinary":
            if target != current or source == target or os.path.samefile(source,target):
                raise ValueError("Legacy ordinary database must be an isolated source copy")
        elif source != target or target != Path(settings.downscale_database_path).resolve():
            raise ValueError("Legacy downscale mapping cannot select another database")
        checked.append({"kind":kind,"source":str(source),"target":str(target)})
    roots = value.get("legacy_roots") or {}
    for name in ("run_dir","output_dir"):
        root = Path(roots[name])
        if not root.is_absolute() or root.resolve() == Path("/"):
            raise ValueError("Legacy task root is unsafe")
    return {"path":str(path.resolve()),"sha256":hashlib.sha256(raw).hexdigest(),
            "database_mappings":checked,"legacy_roots":roots,"tasks":value["tasks"]}


def retirement_databases(settings,tasks,upstream,legacy):
    ids = sorted(t["id"] for t in tasks)
    ordinary_ids = sorted(t["id"] for t in upstream)
    databases = [{"kind":"downscale","path":str(Path(settings.downscale_database_path).resolve()),"task_ids":ids},
                 {"kind":"ordinary","path":str(Path(settings.database_path).resolve()),"task_ids":ordinary_ids}]
    for item in (legacy or {}).get("database_mappings",[]):
        if item["kind"] != "ordinary":
            continue
        old_tasks = read_tasks(Path(item["source"]),"wrf_tasks")
        for task in upstream:
            old = next((row for row in old_tasks if row["id"] == task["id"]),None)
            if (old is None or old.get("owner_sub") != task.get("owner_sub")
                    or old.get("created_at") != task.get("created_at") or old["request"] != task["request"]
                    or old["status"] not in TERMINAL
                    or any(row["id"] != task["id"] and references(row,task["id"]) for row in old_tasks)):
                raise ValueError("Legacy upstream ownership is absent, changed or shared")
        databases.append({"kind":"ordinary","path":item["source"],"task_ids":ordinary_ids})
    unique = {(item["kind"],item["path"]):item for item in databases}
    return [{**item,"records":database_records(Path(item["path"]),set(item["task_ids"]),item["kind"])}
            for item in sorted(unique.values(),key=lambda x:(x["kind"],x["path"]))]


def reused_remote_inventory(client,roots,previous,timeout):
    """Reuse SHA evidence only after every inode, size, mtime and link matches.

    This saves repeated read-only preparation I/O. Apply always hashes all bytes.
    """
    if previous.get("schema") != SCHEMA or previous.get("digest") != manifest_digest(previous):
        raise ValueError("Previous remote inventory digest is invalid")
    expected = []
    for snapshot in previous["remote"]:
        metadata = {**snapshot,"entries":[{k:v for k,v in entry.items() if k != "sha256" or entry["kind"] != "file"}
                                           for entry in snapshot["entries"]]}
        expected.append({"path":snapshot["path"],"metadata_sha256":sha(metadata)})
    current = remote_call(client,"metadata",roots,timeout=timeout)
    if sorted(current,key=lambda x:x["path"]) != sorted(expected,key=lambda x:x["path"]):
        raise RuntimeError("Remote inventory metadata changed; full inventory required")
    return previous["remote"]


def make_manifest(settings,client,engine,task_ids,*,remote=True,timeout=3600,environment_ownership=None,remote_inventory_cache=None):
    ds_all = read_tasks(settings.downscale_database_path,"downscale_tasks")
    wrf_all = read_tasks(settings.database_path,"wrf_tasks")
    downscale,upstream,retained = exclusive_tasks(ds_all,wrf_all,task_ids)
    tasks = downscale+upstream
    legacy = legacy_retirement_assets(settings)
    blockers = []
    if not settings.downscale_retired:
        blockers.append("downscale_creation_is_not_retired")
    if any(t["status"] not in TERMINAL for t in tasks):
        blockers.append("selected_task_is_not_terminal")
    if shared_forcing_references(settings.downscale_database_path,{t["id"] for t in tasks},ds_all+wrf_all):
        blockers.append("owned_forcing_artifact_is_referenced_by_a_retained_task")
    local,remote_roots,terminal_requests = [],[],[]
    for task in tasks:
        identity = task["id"]
        is_ds = identity in set(task_ids)
        local += [str(settings.run_dir/("downscale_jobs" if is_ds else "")/identity),str(settings.output_dir/"runs"/identity),str(settings.catalog_data_root/"WRF"/"workbench"/identity)]
        if legacy:
            old_run = Path(legacy["legacy_roots"]["run_dir"])/("downscale_jobs" if is_ds else "")/identity
            local += [str(old_run),str(Path(legacy["legacy_roots"]["output_dir"])/"runs"/identity)]
            entry = legacy["tasks"].get(identity) or {}
            for archived in entry.get("archived_logs",{}).values():
                if not Path(archived).resolve().is_relative_to(old_run.resolve()):
                    raise ValueError("Archived log escaped its legacy task root")
        if is_ds:
            control, output = legacy_task_paths(settings,identity)
        else:
            control, output = client.task_dir(identity), client.remote_output_dir(identity)
        # Expand only the task-owned native container, never WRFwork or RUNTIME.
        output_parent = str(Path(output).parent) if Path(output).name == "run" else output
        if Path(output_parent).name not in {"DOWNSCALE_"+identity,"WRF_"+identity}:
            raise RuntimeError("Unexpected native output ownership boundary")
        remote_roots += [control,output_parent]
        if not is_ds:
            remote_roots += client.task_artifact_paths(identity)
        terminal_requests.append({"control":control,"attempt":task["runtime"].get("launch_attempt"),"status":task["status"]})
    environment_roots = dedicated_environment_roots(settings,environment_ownership)
    if environment_roots and any(task["id"] not in set(task_ids) for task in ds_all):
        blockers.append("retained_downscale_tasks_may_require_dedicated_environment")
    remote_roots += environment_roots
    local_inventory = [scan_root(p) for p in sorted(set(local))]
    terminals,remote_inventory = [],[]
    if remote:
        terminals = remote_call(client,"terminal",terminal_requests,timeout=min(timeout,60))
        if upstream:
            remote_roots += remote_call(client,"discover",{"root":settings.hpc_remote_dir,"identities":[t["id"] for t in upstream]},timeout=min(timeout,60))
        if environment_roots:
            occupancy = remote_call(client,"occupancy",environment_roots,timeout=min(timeout,60))
            if environment_occupancy_blocked(occupancy,environment_ownership):
                blockers.append("dedicated_environment_in_use_or_process_inspection_incomplete")
        roots = sorted(set(remote_roots))
        remote_inventory = (reused_remote_inventory(client,roots,remote_inventory_cache,timeout)
                            if remote_inventory_cache else remote_call(client,"scan",roots,timeout=timeout))
    else:
        blockers.append("remote_inventory_and_terminal_proof_missing")
    identities = [t["id"] for t in tasks]
    catalog = catalog_snapshot(engine,identities)
    if any(item["references"] for item in catalog):
        blockers.append("catalog_has_shared_references")
    record_databases = retirement_databases(settings,tasks,upstream,legacy)
    records = {item["kind"]:item["records"] for item in record_databases
               if item["path"] == str(Path(settings.downscale_database_path if item["kind"] == "downscale" else settings.database_path).resolve())}
    legacy_binding = ({key:value for key,value in legacy.items() if key != "tasks"} if legacy else None)
    if legacy_binding is not None:
        legacy_binding["task_ids_to_remove"] = sorted(set(legacy["tasks"]) & {task["id"] for task in tasks})
    assets = []
    for key in LEGACY_ENVIRONMENT_SETTINGS:
        path = str(getattr(settings,key,""))
        if not path:
            continue
        assets.append({"setting":key,"path":path,"action":"retain_pending_ownership_review","reason":"Shared Conda/OpenFOAM and scientific assets require separate ownership evidence"})
    if remote:
        assets = remote_call(client,"environment",assets,timeout=min(timeout,60))
    if environment_roots:
        for asset in assets:
            if any(asset["path"] == root or asset["path"].startswith(root+"/") for root in environment_roots):
                asset["action"] = "delete_manifest_bound_dedicated_environment"
    result = {"schema":SCHEMA,"created_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
              "selected_task_ids":sorted(task_ids),"exclusive_upstream_task_ids":sorted(t["id"] for t in upstream),
              "tasks":[{"task_id":t["id"],"owner_sha256":sha(t.get("owner_sub")),"status":t["status"],"updated_at":t["updated_at"],"request_sha256":sha(t["request"]),"runtime_sha256":sha(t["runtime"]),"attempt":t["runtime"].get("launch_attempt"),"result_sha256":sha(t.get("result_json"))} for t in tasks],
              "terminal_evidence":terminals,"local":local_inventory,"remote":remote_inventory,"catalog":catalog,"records":records,
              "record_databases":record_databases,"legacy_asset_mapping":legacy_binding,
              "retained_upstream":retained,"environment_ownership_review":assets,
              "dedicated_environment_ownership":environment_ownership,"blockers":blockers,
              "protected_assets":["shared authoritative forcing pools","WPS/WRF source and toolchains","geographic data","WRF_GPU and preserved research data","unrelated ordinary tasks","terrain/upload authoritative assets"]}
    result["summary"] = {"file_count":sum(x["file_count"] for x in local_inventory+remote_inventory),"symlink_count":sum(x["symlink_count"] for x in local_inventory+remote_inventory),"bytes":sum(x["bytes"] for x in local_inventory+remote_inventory)}
    result["summary"]["logical_task_count"] = len(tasks)
    result["summary"]["record_count"] = sum(row["count"] for item in record_databases for row in item["records"])
    result["digest"] = sha({k:v for k,v in result.items() if k not in {"created_at","digest"}})
    # HTTP observations are diagnostic; exact SQL/file/terminal evidence binds approval.
    result["static_access_observations"] = observe_static_access(catalog,os.getenv("WRF_RETIREMENT_BEARER"))
    return result


def manifest_digest(manifest):
    return sha({k:v for k,v in manifest.items() if k not in {"created_at","digest","static_access_observations"}})


def purge_legacy_mapping(binding):
    """Remove only the approved retired task entries from the shared mapping."""
    if not binding or not binding["task_ids_to_remove"]:
        return
    path = Path(binding["path"])
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
        raise RuntimeError("Legacy asset mapping changed before retirement")
    value = json.loads(path.read_text())
    for identity in binding["task_ids_to_remove"]:
        if identity not in value["tasks"]:
            raise RuntimeError("Legacy task mapping changed before retirement")
        del value["tasks"][identity]
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    descriptor = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(descriptor,"w") as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != binding["sha256"]:
            raise RuntimeError("Legacy asset mapping changed concurrently")
        os.replace(temporary,path)
        directory = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_manifest(manifest,confirmed_digest,settings,client,engine,audit_path,*,timeout=3600,held_lock=None):
    digest = manifest_digest(manifest)
    if manifest.get("schema") != SCHEMA or confirmed_digest != digest or manifest.get("digest") != digest:
        raise ValueError("Explicit confirmation must match the exact manifest digest")
    if manifest.get("blockers"):
        raise RuntimeError("Inventory has unresolved deletion blockers")
    settings = bind_legacy_settings(settings,manifest)
    from output_validation_recovery import held_maintenance_lock
    lock_path = Path(__file__).resolve().parent.parent/".run"/"wrf-maintenance.lock"
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with held_maintenance_lock(lock_path,held_lock):
        return _apply_locked(manifest,digest,settings,client,engine,audit_path,timeout=timeout)


def _apply_locked(manifest,digest,settings,client,engine,audit_path,*,timeout,port_reserved=False):
    from output_validation_recovery import offline_guard,reserve_api_port
    if not port_reserved:
        # Reserve both loopback families for the entire destructive transaction.
        with reserve_api_port(settings.port):
            return _apply_locked(manifest,digest,settings,client,engine,audit_path,
                                 timeout=timeout,port_reserved=True)
    # A historical waiting_restart workflow is not an active computation. Allow
    # it only after fresh, read-only proof that its exact remote session exited;
    # do not rewrite or restart the preserved task to satisfy a local status gate.
    idle_proofs = offline_guard(settings,client,port_reserved=True)
    if Path(audit_path).exists():
        raise RuntimeError("Retirement audit already exists; inspect its phase, do not replay")
    current = make_manifest(settings,client,engine,manifest["selected_task_ids"],timeout=timeout,
                            environment_ownership=manifest.get("dedicated_environment_ownership"))
    if current["digest"] != digest:
        raise RuntimeError("Inventory, ownership, terminal evidence or catalog changed; reapproval required")
    token = os.getenv("WRF_RETIREMENT_BEARER")
    if any(item["record"] or item["assets"] for item in manifest["catalog"]):
        if not token:
            raise RuntimeError("Authenticated static-access verification requires WRF_RETIREMENT_BEARER in the private environment")
        baseline = observe_static_access(manifest["catalog"],token)
        if any(item["status"] not in {200,403,404} for item in baseline):
            raise RuntimeError("Authenticated static-access baseline is unavailable or credentials are invalid")
    audit = {"schema":SCHEMA,"manifest_digest":digest,"approved_at":datetime.now(timezone.utc).isoformat(),
             "tasks":manifest["tasks"],"terminal_evidence":manifest["terminal_evidence"],"summary":manifest["summary"],
             "verified_idle_waiting_tasks":idle_proofs,"phase":"verified"}
    path = Path(audit_path)
    # Store the minimal audit outside every deletion root before the first mutation.
    for root in manifest["local"]:
        if path.resolve().is_relative_to(Path(root["path"])):
            raise ValueError("Audit must be outside retirement assets")
    path.parent.mkdir(parents=True,exist_ok=True)
    def save(first=False):
        target = path if first else path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
        descriptor = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(descriptor,"w") as stream:
                json.dump(audit,stream,ensure_ascii=False,indent=2)
                stream.flush();os.fsync(stream.fileno())
            if not first:
                if path.is_symlink() or json.loads(path.read_text()).get("manifest_digest") != digest:
                    raise RuntimeError("Retirement audit changed concurrently")
                os.replace(target,path)
            directory = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if not first and target.exists():
                target.unlink()
    save(first=True)
    catalog_snapshot(engine,[t["task_id"] for t in manifest["tasks"]],revoke=True,expected=manifest["catalog"])
    audit["phase"]="catalog_revoked";save()
    access = observe_static_access(manifest["catalog"],token)
    if any(item["status"] not in {403,404} for item in access):
        raise RuntimeError("Catalog-revoked static assets remain accessible or access cannot be verified; files retained")
    audit["static_access_denied"] = {"asset_count":len(access),"statuses":sorted(set(item["status"] for item in access))};save()
    remote_call(client,"delete",[{"path":s["path"],"snapshot_sha256":sha(s)} for s in manifest["remote"]],timeout=timeout)
    audit["phase"]="remote_assets_deleted";save()
    for root in manifest["local"]:
        delete_root(root)
    audit["phase"]="local_assets_deleted";save()
    catalog_snapshot(engine,[t["task_id"] for t in manifest["tasks"]],purge=True,expected=manifest["catalog"])
    audit["phase"]="catalog_records_deleted";save()
    databases = manifest.get("record_databases") or [
        {"kind":"downscale","path":settings.downscale_database_path,"task_ids":[t["task_id"] for t in manifest["tasks"]],"records":manifest["records"]["downscale"]},
        {"kind":"ordinary","path":settings.database_path,"task_ids":manifest["exclusive_upstream_task_ids"],"records":manifest["records"]["ordinary"]}]
    for item in databases:
        database_records(Path(item["path"]),set(item["task_ids"]),item["kind"],delete=True,expected=item["records"])
    audit["phase"]="task_records_deleted";save()
    purge_legacy_mapping(manifest.get("legacy_asset_mapping"))
    audit["phase"]="legacy_task_mappings_removed";save()
    remaining = remote_call(client,"scan",[s["path"] for s in manifest["remote"]],timeout=min(timeout,60))
    if any(s["exists"] for s in remaining) or any(os.path.lexists(s["path"]) for s in manifest["local"]):
        raise RuntimeError("Retirement verification found remaining assets")
    final_catalog = catalog_snapshot(engine,[t["task_id"] for t in manifest["tasks"]])
    if any(c["record"] or c["assets"] for c in final_catalog):
        raise RuntimeError("Retirement verification found remaining catalog records")
    for item in databases:
        if any(r["count"] for r in database_records(Path(item["path"]),set(item["task_ids"]),item["kind"])):
            raise RuntimeError("Retirement verification found remaining task records")
    audit["phase"]="complete";audit["completed_at"]=datetime.now(timezone.utc).isoformat();save()
    return audit
