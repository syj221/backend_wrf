#!/usr/bin/env python3
"""Shared durable process supervision; no business-specific execution policy."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path


TERMINAL = {"succeeded", "failed", "cancelled"}


def save(path, value):
    temporary = path.with_suffix(".new")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def identity(pid):
    stat = Path("/proc/{}/stat".format(pid)).read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "start_ticks": stat[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def inspect(root, token):
    path = root / "launches" / token / "state.json"
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        # A read can race with a state-file or storage-alias replacement.
        # Absence is uncertainty, never permission to launch again.
        return {"status": "unknown", "attempt": token}
    if state.get("cancelled") and state.get("cancellation_verified"):
        state["status"] = "cancelled"
    cancellation = path.parent / "cancellation.json"
    if cancellation.is_file():
        request = json.loads(cancellation.read_text())
        if request.get("status") != "complete":
            state["status"] = "cancel_pending" if process_alive(request.get("process")) else "unknown"
            state["cancel_requested"] = True
            return state
    if state.get("status") not in TERMINAL:
        alive = process_alive(state.get("process"))
        state["status"] = "running" if alive else "unknown"
    log = path.parent / "service.log"
    if log.is_file():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 32000))
            state["log"] = stream.read().decode("utf-8", "replace")
    return state


def process_alive(expected):
    try:
        if identity(expected["pid"]) != expected:
            return False
        return Path("/proc/{}/stat".format(expected["pid"])).read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (TypeError, KeyError, OSError, ValueError, IndexError):
        return False


def session_processes(session):
    """Only enumerate the launch's dedicated session; zombies cannot compute."""
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[3]) != session or fields[0] == "Z":
                continue
            if path.stat().st_uid != os.getuid():
                raise RuntimeError("Session contains a process owned by another user")
            result.append(identity(int(path.name)))
        except FileNotFoundError:
            continue
    return result


def cancel(root, token):
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        attempt = root / "launches" / token
        if not attempt.exists():
            # A persistent tombstone closes the intent-before-launch race.
            attempt.mkdir(parents=True)
            save(attempt / "state.json", {"status": "failed", "attempt": token,
                                         "cancelled": True, "cancellation_verified": True,
                                         "reason": "cancelled_before_launch"})
            return inspect(root, token)
        if (attempt / "cancellation.json").exists():
            request = json.loads((attempt / "cancellation.json").read_text())
            if request.get("status") == "complete" or process_alive(request.get("process")):
                return inspect(root, token)
        else:
            request = {"status": "pending"}
        state = json.loads((attempt / "state.json").read_text())
        if state.get("status") in TERMINAL:
            return inspect(root, token)
        expected = state.get("process")
        if not expected:
            # The launch claim is durable, but its child has not registered.
            # Persist intent only; execute() will consume it before any command.
            save(attempt / "cancellation.json", request)
            return {"status": "cancel_pending", "attempt": token}
        if not process_alive(expected) or os.getsid(expected["pid"]) != expected["pid"]:
            raise RuntimeError("Launch session identity is unknown; cancellation refused")
        if request.get("target") and request["target"] != expected:
            raise RuntimeError("Cancellation target identity changed")
        request["target"] = expected
        save(attempt / "cancellation.json", request)
        with (attempt / "cancel.log").open("ab") as log:
            worker = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                              "cancel-execute", str(root), token],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
            request["process"] = identity(worker.pid)
            save(attempt / "cancellation.json", request)
        return {"status": "cancel_pending", "attempt": token}


def cancel_execute(root, token):
    attempt = root / "launches" / token
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        request = json.loads((attempt / "cancellation.json").read_text())
        if request.get("process") != identity(os.getpid()):
            raise RuntimeError("Cancellation worker identity changed")
    expected = request["target"]
    session = expected["pid"]
    if not process_alive(expected):
        remaining = session_processes(session)
        if any(member not in request.get("authorized_members", []) for member in remaining):
            raise RuntimeError("Original supervisor identity lost with remaining processes")
    elif os.getsid(session) != session:
        raise RuntimeError("Launch session changed")
    deadline = time.monotonic() + 10
    for iteration in range(40):
        members = session_processes(session)
        if not members:
            state = json.loads((attempt / "state.json").read_text())
            # Older status readers recognize failed as terminal. New readers
            # expose the verified cancellation without stranding old workers.
            state.update(status="failed", cancelled=True, cancellation_verified=True,
                         cancellation_completed_at=time.time())
            save(attempt / "state.json", state)
            request["status"] = "complete"
            save(attempt / "cancellation.json", request)
            return
        request["authorized_members"] = members
        save(attempt / "cancellation.json", request)
        sig = signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL
        for member in members:
            if process_alive(member):
                try:
                    os.kill(member["pid"], sig)
                    if sig == signal.SIGTERM:
                        os.kill(member["pid"], signal.SIGCONT)
                except ProcessLookupError:
                    pass
        time.sleep(0.5)
    raise RuntimeError("Session has not exited; cancellation remains unconfirmed")


def legacy_status(root):
    if (root / "exit.code").is_file():
        code = int((root / "exit.code").read_text().strip())
        return {"status": "succeeded" if code == 0 else "failed", "exit_code": code}
    # Old PID files have no generation identity. Never authorize a new launch.
    return {"status": "unknown"}


def checked_pool_path(path, data):
    """Bind reservations to owned, non-symlink task control directories."""
    relative = path.relative_to(data)
    if len(relative.parts) != 3 or relative.parts[0] != 'tasks' or relative.parts[2] != 'control':
        raise RuntimeError('Invalid compute reservation task root')
    current = data
    for part in ('',) + relative.parts:
        if part: current = current / part
        info = current.lstat()
        if current.is_symlink() or not current.is_dir() or info.st_uid != os.getuid() or info.st_mode & 2:
            raise RuntimeError('Unsafe compute reservation directory')


def safe_terminal(root, token):
    state = inspect(root, token)
    if state.get('status') not in TERMINAL:
        return False
    process = state.get('process')
    if not process:
        return bool(state.get('cancellation_verified'))
    if process_alive(process) or session_processes(process['pid']):
        return False
    return True


def pool_state(data):
    path = data / 'compute-pool.json'
    if path.exists() and (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 2):
        raise RuntimeError('Unsafe compute pool file')
    value = json.loads(path.read_text()) if path.exists() else {'schema':'wrf-compute-pool-1','reservations':{}}
    if value.get('schema') != 'wrf-compute-pool-1' or not isinstance(value.get('reservations'), dict):
        raise RuntimeError('Unknown compute pool schema')
    for item in value['reservations'].values():
        root = Path(item['root'])
        # Removed task roots are only acceptable for already released audits.
        if item.get('released_at'): continue
        if (not re.fullmatch(r'[a-f0-9]{32}', item['attempt']) or type(item['cpu_count']) is not int
                or type(item['memory_bytes']) is not int or item['cpu_count'] < 1 or item['memory_bytes'] < 2 * 1024**3
                or any(type(cpu) is not int or cpu < 0 for cpu in item['cpu_ids']) or len(set(item['cpu_ids'])) != item['cpu_count']):
            raise RuntimeError('Invalid retained compute reservation')
        checked_pool_path(root, data)
        info = root.stat()
        if item.get('directory_identity') != [info.st_dev, info.st_ino]:
            raise RuntimeError('Compute reservation directory changed')
        if safe_terminal(root, item['attempt']):
            item['released_at'] = time.time()
    active = [item for item in value['reservations'].values() if not item.get('released_at')]
    if sum(item['cpu_count'] for item in active) != len({cpu for item in active for cpu in item['cpu_ids']}):
        raise RuntimeError('Overlapping retained compute reservations')
    return value


def pool_lock(data):
    info = data.lstat()
    if data.is_symlink() or not data.is_dir() or info.st_uid != os.getuid() or info.st_mode & 2:
        raise RuntimeError('Unsafe compute pool root')
    fd = os.open(str(data/'compute.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 2:
        os.close(fd)
        raise RuntimeError('Unsafe compute pool lock')
    return os.fdopen(fd, 'r+')


def pool_report(data, *, inspect_legacy=False):
    with pool_lock(data) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = pool_state(data)
        legacy_blocked = legacy_lease_blocked(data, value) if inspect_legacy else None
        save(data/'compute-pool.json', value)
        active = [item for item in value['reservations'].values() if not item.get('released_at')]
        return {'schema':value['schema'], 'active':active, 'released_count':len(value['reservations'])-len(active),'legacy_blocked':legacy_blocked}


def release_terminal_reservation(root, token, state):
    if state.get('status') not in TERMINAL or root.name != 'control' or root.parent.parent.name != 'tasks':
        return state
    data = root.parent.parent.parent
    path = data/'compute-pool.json'
    if not path.exists(): return state
    value = json.loads(path.read_text())
    if root.parent.name+':'+token not in value.get('reservations', {}): return state
    if safe_terminal(root, token):
        pool_report(data)
        state['compute_resources_released'] = True
    else:
        # The wrapper/session must also exit before local completion or deletion.
        state['status'] = 'running' if process_alive(state.get('process')) else 'unknown'
    return state


def legacy_lease_blocked(data, value):
    """Retain terminal evidence before later approved deletion removes its root."""
    data = data.resolve()
    legacy = data / 'compute-lease.json'
    if not legacy.exists(): return False
    info = legacy.lstat()
    if legacy.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 2:
        raise RuntimeError('Unsafe legacy lease')
    previous = json.loads(legacy.read_text())
    if not re.fullmatch(r'[a-f0-9]{32}', previous.get('attempt', '')):
        raise RuntimeError('Invalid legacy lease attempt')
    digest = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
    root = Path(previous['root']).resolve()
    if root.parent.parent != data / 'tasks' or root.name != 'control':
        raise RuntimeError('Invalid legacy lease root')
    evidence = value.setdefault('legacy_released', {}).get(digest)
    if not root.exists():
        if evidence and evidence.get('root') == str(root) and evidence.get('attempt') == previous['attempt'] and evidence.get('terminal_verified') is True:
            return False
        raise RuntimeError('Missing legacy root without retained terminal evidence')
    checked_pool_path(root, data)
    identity = [root.stat().st_dev, root.stat().st_ino]
    if evidence and evidence.get('directory_identity') != identity:
        raise RuntimeError('Legacy lease directory changed')
    if not safe_terminal(root, previous['attempt']): return True
    value['legacy_released'][digest] = {'root':str(root),'attempt':previous['attempt'],
        'directory_identity':identity,'terminal_verified':True,'verified_at':time.time()}
    return False


def pool_launch(data, root, token, command):
    checked_pool_path(root, data)
    count = min(4, max(1, int(os.environ['WRF_COMPUTE_MAX_TASKS'])))
    cpus = int(os.environ['WRF_COMPUTE_CPU_BUDGET'])
    memory = int(os.environ['WRF_COMPUTE_MEMORY_BUDGET'])
    if cpus < 1 or memory < 2 * 1024**3: raise RuntimeError('Invalid compute budget')
    path = data / 'compute-pool.json'
    with pool_lock(data) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = pool_state(data)
        key = root.parent.name + ':' + token
        old = value['reservations'].get(key)
        if old:
            if old['root'] != str(root) or old['cpu_count'] != cpus or old['memory_bytes'] != memory:
                raise RuntimeError('Compute reservation identity changed')
            # A durable reservation with an uncertain launch is never replayed.
            save(path, value)
            return inspect(root, token)
        reason = '旧发布计算租约仍活动或身份待确认' if legacy_lease_blocked(data, value) else ''
        active = [item for item in value['reservations'].values() if not item.get('released_at')]
        cpu_ids = sorted(os.sched_getaffinity(0))
        headroom = int(os.environ.get('WRF_COMPUTE_CPU_HEADROOM', '2'))
        memory_headroom = int(os.environ.get('WRF_COMPUTE_MEMORY_HEADROOM', str(2 * 1024**3)))
        available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))) * 1024
        used = {cpu for item in active for cpu in item['cpu_ids']}
        free = [cpu for cpu in cpu_ids[:max(0,len(cpu_ids)-headroom)] if cpu not in used]
        observations = [inspect(Path(item['root']), item['attempt']) for item in active]
        if any(state['status'] == 'unknown' or (state['status'] in TERMINAL and not state.get('process') and not state.get('cancellation_verified')) for state in observations): reason = reason or '已有计算尝试身份待确认，保留资源'
        if len(active) >= count: reason = reason or '4个计算名额已占用'
        if len(free) < cpus: reason = reason or 'CPU预留不足'
        if available < memory + memory_headroom + sum(item['memory_bytes'] for item in active): reason = reason or '可用内存不足，等待预留释放'
        if reason:
            save(path, value)
            return {'status':'waiting_resources','attempt':token,'reason':reason,'started':False}
        item = {'root':str(root),'attempt':token,'cpu_count':cpus,'cpu_ids':free[:cpus],
                'memory_bytes':memory,'accepted_at':time.time(),
                'directory_identity':[root.stat().st_dev, root.stat().st_ino]}
        value['reservations'][key] = item
        save(path, value)
        previous_ids = os.environ.get('WRF_COMPUTE_CPU_IDS')
        os.environ['WRF_COMPUTE_CPU_IDS'] = ','.join(str(cpu) for cpu in item['cpu_ids'])
        try:
            return launch_claim(root, token, command)
        finally:
            if previous_ids is None: os.environ.pop('WRF_COMPUTE_CPU_IDS', None)
            else: os.environ['WRF_COMPUTE_CPU_IDS'] = previous_ids


def launch(root, token, command):
    root.mkdir(parents=True, exist_ok=True)
    resource = os.environ.get('WRF_COMPUTE_RESOURCE_ROOT')
    if resource:
        lease = Path(resource)
        if int(os.environ.get('WRF_COMPUTE_MAX_TASKS', '1')) > 1:
            return pool_launch(lease.resolve(), root.resolve(), token, command)
        with (lease/'compute.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = lease/'compute-lease.json'
            if path.exists():
                previous = json.loads(path.read_text())
                if previous != {'root':str(root), 'attempt':token}:
                    if inspect(Path(previous['root']), previous['attempt'])['status'] not in TERMINAL:
                        raise RuntimeError('Ordinary compute resource is occupied or unknown')
            save(path, {'root':str(root), 'attempt':token})
            # The resource reservation is durable before any task process.
            return launch_claim(root, token, command)
    return launch_claim(root, token, command)


def launch_claim(root, token, command):
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        attempt = root / "launches" / token
        if attempt.exists():
            return inspect(root, token)
        active = root / "active-launch.json"
        if active.exists():
            previous = json.loads(active.read_text())["attempt"]
            if inspect(root, previous)["status"] not in TERMINAL:
                raise RuntimeError("Previous launch is running or unknown; replay refused")
        elif (root / "service.pid").exists() and legacy_status(root)["status"] not in TERMINAL:
            raise RuntimeError("Legacy process has no terminal evidence; replay refused")
        attempt.mkdir(parents=True)
        # Commit the claim before creating any process. A crash leaves unknown.
        save(attempt / "state.json", {"status": "unknown", "attempt": token})
        save(active, {"attempt": token})
        with (attempt / "service.log").open("ab") as output:
            subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                              "execute", str(root), token, command],
                             stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                             start_new_session=True, close_fds=True)
    # The child registers under launch.lock; never wait while holding that lock.
    deadline = time.monotonic() + 1
    while True:
        state = inspect(root, token)
        if state["status"] != "unknown" or time.monotonic() >= deadline:
            return state
        time.sleep(0.02)


def execute(root, token, command):
    if os.environ.get('WRF_COMPUTE_CPU_IDS'):
        os.sched_setaffinity(0, {int(cpu) for cpu in os.environ['WRF_COMPUTE_CPU_IDS'].split(',')})
        os.environ['OMP_NUM_THREADS'] = '1'
        # Open MPI otherwise rebinds np<=2 to core zero outside our cpuset.
        os.environ['OMPI_MCA_hwloc_base_binding_policy'] = 'none'
    path = root / "launches" / token / "state.json"
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = json.loads(path.read_text())
        if previous.get("status") in TERMINAL:
            return
        if previous.get("process"):
            raise RuntimeError("Launch child identity is already registered; replay refused")
        cancellation = path.parent / "cancellation.json"
        if cancellation.exists():
            request = json.loads(cancellation.read_text())
            previous.update(status="failed", cancelled=True, cancellation_verified=True,
                            reason="cancelled_before_command")
            save(path, previous)
            request["status"] = "complete"
            save(cancellation, request)
            return
        state = {"status": "running", "attempt": token, "process": identity(os.getpid())}
        save(path, state)
        save(root / "process.json", state["process"])
        (root / "service.pid").write_text(str(os.getpid()))
    code = subprocess.call(["bash", "-c", command], cwd=str(root))
    state.update(status="succeeded" if code == 0 else "failed", exit_code=code)
    save(path, state)
    (root / "exit.code").write_text(str(code))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["launch", "status", "execute", "cancel", "cancel-execute", "resources"])
    parser.add_argument("root", type=Path)
    parser.add_argument("token")
    parser.add_argument("command", nargs="?", default="")
    args = parser.parse_args()
    args.root = args.root.resolve()
    if not re.fullmatch(r"[a-f0-9]{32}", args.token):
        raise ValueError("Invalid attempt token")
    if args.action == 'resources':
        print(json.dumps(pool_report(args.root, inspect_legacy=True)))
    elif args.action == "execute":
        execute(args.root, args.token, args.command)
    elif args.action == "cancel-execute":
        cancel_execute(args.root, args.token)
    elif args.action == "cancel":
        print(json.dumps(release_terminal_reservation(args.root, args.token, cancel(args.root, args.token))))
    else:
        result = launch(args.root, args.token, args.command) if args.action == "launch" else inspect(args.root, args.token)
        print(json.dumps(release_terminal_reservation(args.root, args.token, result)))


if __name__ == "__main__":
    main()
