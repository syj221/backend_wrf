"""Operator-only file-boundary handoff. No enqueue, file removal or queue edits."""
import contextlib
import ctypes
import platform
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import time


def pidfd_open(pid):
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid)
    return pidfd_syscall(434, pid, 0)


def pidfd_signal(fd, sig):
    if hasattr(signal, 'pidfd_send_signal'):
        return signal.pidfd_send_signal(fd, sig)
    return pidfd_syscall(424, fd, sig, 0, 0)


def pidfd_syscall(number, *args):
    # Ubuntu's kernel supports pidfd even when Python was built without the
    # libc declarations. Never fall back to kill(pid); bind the same kernel fd.
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'aarch64'}:
        raise RuntimeError('unsupported native pidfd ABI')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(number), *(ctypes.c_long(value) for value in args))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def identity(pid):
    proc = Path('/proc') / str(pid)
    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19],
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'argv': (proc / 'cmdline').read_bytes().decode().strip('\0').split('\0'),
            'exe': str((proc / 'exe').resolve()), 'uid': proc.stat().st_uid}


def check_identity(expected):
    if identity(expected['pid']) != expected or expected['uid'] != os.getuid():
        raise RuntimeError('worker identity changed; untouched')


def verify_module(path, expected_sha):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
        raise RuntimeError('frozen queue module changed; untouched')


def children(pid):
    return {int(value) for task in (Path('/proc') / str(pid) / 'task').iterdir()
            for value in (task / 'children').read_text().split()}


def marker(root):
    path = root / '.pool-worker.lockdir'
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError('unsafe worker marker')
    owner = json.loads((path / 'owner.json').read_text())
    if owner.get('protocol') != 'wrf-pool-worker-2' or owner.get('root') != str(path):
        raise RuntimeError('unknown worker marker')
    return {'owner': owner, 'device': path.stat().st_dev, 'inode': path.stat().st_ino}


def prepare(roots, old_module, new_module):
    old_module, new_module = str(Path(old_module)), str(Path(new_module))
    value = {'schema': 'wrf-pool-handoff-1', 'old_module': old_module,
             'new_module': new_module, 'old_sha': hashlib.sha256(Path(old_module).read_bytes()).hexdigest(),
             'new_sha': hashlib.sha256(Path(new_module).read_bytes()).hexdigest(), 'workers': []}
    for source, raw in sorted(roots.items()):
        if source not in {'gfs', 'ecmwf'}:
            raise ValueError('unknown source')
        root = Path(raw).resolve(strict=True)
        if root.name != source or root.stat().st_uid != os.getuid():
            raise RuntimeError('pool ownership mismatch')
        recorded = marker(root)
        process = identity(recorded['owner']['pid'])
        argv = process['argv']
        if (len(argv) < 7 or argv[1:3] != [old_module, 'worker'] or
                argv[argv.index('--source') + 1] != source or
                Path(argv[argv.index('--root') + 1]).resolve() != root):
            raise RuntimeError('worker command does not match the exact old pool')
        if process['uid'] != os.getuid():
            raise RuntimeError('worker ownership mismatch')
        value['workers'].append({'source': source, 'root': str(root),
                                 'root_identity': [root.stat().st_dev, root.stat().st_ino, root.stat().st_uid],
                                 'marker': recorded, 'process': process})
    value['digest'] = digest(value)
    return value


def verify_unacted_ecmwf(previous, current):
    """Read-only proof; never repairs or replays the predecessor transaction."""
    path=Path(previous['audit'])
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('unsafe predecessor audit')
    raw=path.read_bytes();record=json.loads(raw)
    if (record.get('status')!='needs_review' or
            record.get('error')!='TimeoutError: file boundary wait exhausted; remaining workers untouched' or
            record.get('plan')!=previous['plan'] or record.get('plan_digest')!=previous['plan']['digest'] or
            set(record.get('providers',{}))!={'gfs'} or
            record['providers']['gfs'].get('status')!='new_worker_started'):
        raise RuntimeError('predecessor has unknown or ECMWF actions; refuse new plan')
    old=next(w for w in previous['plan']['workers'] if w['source']=='ecmwf')
    if current['workers']!=[old]:
        raise RuntimeError('original untouched ECMWF identity changed')
    check_identity(old['process'])
    if marker(Path(old['root']))!=old['marker']:
        raise RuntimeError('original ECMWF marker changed')
    fence=path.with_name(path.stem+'.fence.json')
    fence_raw=fence.read_bytes();evidence=json.loads(fence_raw)
    if evidence.get('plan_digest')!=record['plan_digest'] or evidence.get('status')!='released_needs_review':
        raise RuntimeError('original boundary fence has not ended')
    controller=evidence['controller']
    try:
        alive=identity(controller['pid'])
    except FileNotFoundError:
        alive=None
    if alive==controller:
        fd=pidfd_open(controller['pid'])
        try:
            poll=select.poll();poll.register(fd,select.POLLIN)
            if not poll.poll(0):raise RuntimeError('original controller is still active')
        finally:os.close(fd)
    return {'audit':str(path),'audit_sha256':hashlib.sha256(raw).hexdigest(),
            'fence_sha256':hashlib.sha256(fence_raw).hexdigest(),
            'plan_digest':record['plan_digest'],'ecmwf_actions':False,'previous':previous}


def save_audit(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def handoff(plan, queue_module, audit, expected_digest, timeout=900):
    """Fence each source at a file boundary; stop only exact idle workers."""
    unsigned = {k: v for k, v in plan.items() if k != 'digest'}
    if digest(unsigned) != expected_digest or plan.get('digest') != expected_digest:
        raise RuntimeError('handoff digest mismatch')
    sources=[w['source'] for w in plan['workers']]
    if sources not in [['gfs'],['ecmwf'],['ecmwf','gfs']]:
        raise RuntimeError('handoff requires exact approved providers')
    if plan.get('minimum_wait_seconds',0)>timeout:
        raise RuntimeError('wait budget does not cover the approved single-file limit')
    if plan.get('predecessor'):
        proof=verify_unacted_ecmwf(plan['predecessor']['previous'],plan)
        if proof!=plan['predecessor']:raise RuntimeError('predecessor proof changed')
    verify_module(plan['old_module'], plan['old_sha'])
    verify_module(plan['new_module'], plan['new_sha'])
    audit = Path(audit)
    if audit.exists() or audit.is_symlink():
        raise RuntimeError('existing handoff audit; inspect, never replay')
    audit.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = audit.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('unsafe audit directory')
    report = {'schema': 'wrf-pool-handoff-audit-1', 'plan_digest': expected_digest,
              'status': 'waiting_file_boundary', 'plan': plan, 'started_at': time.time()}
    save_audit(audit, report)
    deadline = time.monotonic() + timeout
    roots = [Path(w['root']) for w in plan['workers']]
    if any(root.parent!=roots[0].parent for root in roots):
        raise RuntimeError('provider roots do not share the approved parent')
    fence_stack=contextlib.ExitStack()
    previous_alarm=None
    try:
        if sources in [['ecmwf'], ['gfs']] or plan.get('source_transfer_fences'):
            def boundary_expired(*unused):
                raise TimeoutError('single-file boundary wait exhausted; worker untouched')
            previous_alarm=signal.signal(signal.SIGALRM,boundary_expired)
            signal.setitimer(signal.ITIMER_REAL,timeout)
            # Blocking on the inherited old slot lets the current curl finish
            # and then prevents the old worker from taking the next file.
            if not plan.get('source_transfer_fences'):
                fence_stack.enter_context(queue_module.kernel_guard((roots[0]/'.pool-download-slot') if sources==['gfs'] else (roots[0].parent/'.pool-download-slot'),blocking=True))
            if plan.get('source_transfer_fences'):
                report.update(provider_slot_wait_started_at=time.time())
            else:
                report.update(legacy_boundary_acquired_at=time.time())
            save_audit(audit,report)
        report['providers'] = {}
        pending = list(plan['workers'])
        while pending and time.monotonic() < deadline:
            for worker in list(pending):
                root = Path(worker['root'])
                with contextlib.ExitStack() as gates:
                    if plan.get('source_transfer_fences'):
                        signal.setitimer(signal.ITIMER_REAL,max(.001,deadline-time.monotonic()))
                        gates.enter_context(queue_module.kernel_guard(root/'.pool-download-slot',blocking=True))
                    verify_module(plan['old_module'], plan['old_sha'])
                    verify_module(plan['new_module'], plan['new_sha'])
                    check_identity(worker['process'])
                    if [root.stat().st_dev, root.stat().st_ino, root.stat().st_uid] != worker['root_identity']:
                        raise RuntimeError('pool directory identity changed; untouched')
                    if marker(root) != worker['marker']:
                        raise RuntimeError('worker marker changed; untouched')
                    # Do not wait while holding a state lock: the active transfer
                    # must be able to commit its result before releasing its cycle.
                    state_guard = gates.enter_context(queue_module.exclusive(root / '.pool-queue.lock', blocking=False))
                    if state_guard is None:
                        continue
                    state = json.loads((root / '.pool-queue.json').read_text())
                    if state.get('source') != worker['source'] or state.get('version') != 1:
                        raise RuntimeError('unknown queue state; untouched')
                    ready = True
                    for cycle, item in state['cycles'].items():
                        queue_module.safe_cycle(cycle)
                        if item.get('publication') == 'probing' or any(
                                entry.get('status') == 'downloading' for entry in item.get('files', {}).values()):
                            ready = False
                            break
                        hours = list(range(73)) if worker['source']=='gfs' else list(range(0,73,3))
                        if all(item.get('files',{}).get(f'{hour:03d}',{}).get('status')=='ready' for hour in hours):
                            # The last curl may have ended while its parent is still
                            # hashing the complete-cycle manifest. Do not interrupt it.
                            try:
                                manifest=json.loads((root/cycle/'manifest.json').read_text())
                            except (FileNotFoundError,json.JSONDecodeError):
                                ready=False
                                break
                            if not (manifest.get('complete') is True and manifest.get('provider')==worker['source']
                                    and manifest.get('cycle')==cycle and manifest.get('forecast_hours')==hours):
                                ready=False
                                break
                        gate = gates.enter_context(queue_module.exclusive(root / ('.pool-cycle-' + cycle + '.lock'), blocking=False))
                        if gate is None:
                            ready = False
                            break
                    if not ready or children(worker['process']['pid']):
                        continue
                    # The frozen legacy module acquires this exact state lock
                    # before probes, and these cycle gates before any file curl.
                    # Holding both excludes a new write after the child check.
                    descriptor = pidfd_open(worker['process']['pid'])
                    gates.callback(os.close, descriptor)
                    check_identity(worker['process'])
                    if children(worker['process']['pid']):
                        continue
                    entry = {'status': 'boundary_verified', 'boundary_at': time.time(),
                             'queue_digest': digest(state), 'queue': state}
                    report['providers'][worker['source']] = entry
                    report.update(status='stopping_idle_worker')
                    save_audit(audit, report)
                    pidfd_signal(descriptor, signal.SIGTERM)
                    poll = select.poll()
                    poll.register(descriptor, select.POLLIN)
                    if not poll.poll(15000):
                        raise RuntimeError('idle worker did not exit; no force-kill')
                    entry.update(status='old_worker_exited', stopped_at=time.time())
                    save_audit(audit, report)
                # Resume only the original accepted work order, without enqueue.
                argv = list(worker['process']['argv'])
                argv[0], argv[1] = worker['process']['exe'], plan['new_module']
                log = root / 'logs' / 'pool_worker.out'
                if log.parent.is_symlink() or log.parent.resolve() != root / 'logs':
                    raise RuntimeError('unsafe worker log parent')
                fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid():
                        raise RuntimeError('unsafe worker log')
                    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                                               close_fds=True, start_new_session=True)
                    entry.update(status='new_worker_started', spawned_pid=process.pid)
                finally:
                    os.close(fd)
                pending.remove(worker)
                report.update(status='waiting_file_boundary' if pending else 'new_workers_started')
                save_audit(audit, report)
            if pending:
                time.sleep(.2)
        if pending:
            raise TimeoutError('file boundary wait exhausted; remaining workers untouched')
        report.update(status='new_workers_started', finished_at=time.time())
        save_audit(audit, report)
        return report
    except BaseException as error:
        report.update(status='needs_review', error=type(error).__name__ + ': ' + str(error), finished_at=time.time())
        save_audit(audit, report)
        raise
    finally:
        fence_stack.close()
        if previous_alarm is not None:
            signal.setitimer(signal.ITIMER_REAL,0)
            signal.signal(signal.SIGALRM,previous_alarm)


def operator_main():
    """Explicit operator maintenance entry, defaulting to a read-only plan."""
    import argparse
    import fcntl
    import shlex
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--digest')
    parser.add_argument('--timeout', type=int)
    parser.add_argument('--source', choices=['both','ecmwf','gfs'], default='both')
    parser.add_argument('--previous-plan', type=Path)
    args = parser.parse_args()
    args.timeout=args.timeout if args.timeout is not None else 4200 if args.source in {'ecmwf','gfs'} else 900
    if not 1 <= args.timeout <= 7200 or (args.apply and not args.digest):
        parser.error('apply requires original digest; timeout must be 1..7200 seconds')
    project = Path(__file__).resolve().parents[2]
    target = args.plan.resolve()
    if project / '.run' not in target.parents:
        parser.error('private plan must be inside the project .run directory')
    sys.path.insert(0, str(project / 'backend_wrf'))
    from config import settings
    from hpc_transport import HpcClient
    from runtime_release import require_ready
    client = HpcClient(settings)
    # Each bridge is tied to inspected immutable modules and a known slot protocol.
    modern = settings.hpc_runtime_release == 'ordinary-20261020-19'
    if modern:
        if args.timeout < 4200 or args.previous_plan:
            raise RuntimeError('Modern provider bridge requires a 4200-second budget and no legacy transaction')
        old_release = 'ordinary-20261019-18'
    elif args.source == 'gfs':
        bridges = {'ordinary-20261019-17': 'ordinary-20261016-16',
                   'ordinary-20261019-18': 'ordinary-20261019-17'}
        if settings.hpc_runtime_release not in bridges or args.timeout < 4200 or args.previous_plan:
            raise RuntimeError('GFS bridge requires an approved immutable target and 4200-second budget')
        old_release = bridges[settings.hpc_runtime_release]
    else:
        if settings.hpc_runtime_release != 'ordinary-20261016-16':
            raise RuntimeError('unexpected target runtime; inspect before handoff')
        old_release = 'ordinary-20261014-15'
    old = settings.hpc_release_root + '/releases/' + old_release + '/scripts/forcing_pool_queue.py'
    new = settings.hpc_release_root + '/releases/' + settings.hpc_runtime_release + '/scripts/forcing_pool_queue.py'
    roots = {'gfs': settings.hpc_gfs_dir, 'ecmwf': settings.hpc_ec_dir}
    if args.source=='gfs':
        roots={'gfs':settings.hpc_gfs_dir}
    previous=None
    if args.source=='ecmwf':
        roots={'ecmwf':settings.hpc_ec_dir}
    if args.source=='ecmwf' and not modern:
        if not args.previous_plan or args.timeout<4200:
            parser.error('Legacy ECMWF requires predecessor evidence and at least 4200 seconds')
        previous=json.loads(args.previous_plan.read_text())
        prior=previous['plan'];expected=settings.hpc_release_root+'/maintenance/pool-handoff/'+prior['digest']+'.json'
        if previous['audit']!=expected or digest({k:v for k,v in prior.items() if k!='digest'})!=prior['digest']:
            raise RuntimeError('predecessor binding changed')
    header = 'import importlib.util,json\n' + Path(__file__).read_text().split("\nif __name__ == '__main__':", 1)[0] + '\n'
    env = '. ' + shlex.quote(settings.hpc_release_root + '/releases/' + settings.hpc_runtime_release + '/runtime.env') + ' && '
    with (project / '.run/wrf-maintenance.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ready = require_ready(client, settings, force=True)
        if not args.apply:
            program = header + '\nplan=prepare(' + repr(roots) + ',' + repr(old) + ',' + repr(new) + ')\n'
            if modern:
                program+='plan.pop("digest");plan["minimum_wait_seconds"]=4200;plan["source_transfer_fences"]=True;plan["digest"]=digest(plan)\n'
            elif args.source=='gfs':
                program+='plan.pop("digest");plan["minimum_wait_seconds"]=4200;plan["digest"]=digest(plan)\n'
            if previous:
                program+='plan.pop("digest");plan["predecessor"]=verify_unacted_ecmwf(json.loads('+repr(json.dumps(previous))+'),plan);plan["minimum_wait_seconds"]=4200;plan["digest"]=digest(plan)\n'
            program+='print(json.dumps(plan))'
            plan = json.loads(client.run(env + shlex.quote(settings.hpc_runtime_python) + ' -B -c ' + shlex.quote(program), timeout=60))
            old_manifest = json.loads(client.run('cat ' + shlex.quote(str(Path(old).parents[1] / 'manifest.json')), timeout=30))
            if old_manifest['files']['scripts/forcing_pool_queue.py'] != plan['old_sha']:
                raise RuntimeError('legacy module differs from frozen manifest')
            envelope = {'plan': plan, 'target_fingerprint': ready['fingerprint'],
                        'audit': settings.hpc_release_root + '/maintenance/pool-handoff/' + plan['digest'] + '.json'}
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(envelope, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            print(json.dumps({'status': 'planned', 'digest': plan['digest'], 'target_fingerprint': ready['fingerprint']}))
        else:
            envelope = json.loads(target.read_text())
            plan = envelope['plan']
            expected_audit = settings.hpc_release_root + '/maintenance/pool-handoff/' + args.digest + '.json'
            if (envelope['target_fingerprint'] != ready['fingerprint'] or plan['digest'] != args.digest or
                    envelope['audit'] != expected_audit or plan['old_module'] != old or plan['new_module'] != new):
                raise RuntimeError('approved handoff binding changed')
            program = header + '\nroots=' + repr(roots) + '\nplan=json.loads(' + repr(json.dumps(plan)) + ')\n'
            program += "assert {w['source']:w['root'] for w in plan['workers']}=={s:str(Path(r).resolve(strict=True)) for s,r in roots.items()}\n"
            program += 'spec=importlib.util.spec_from_file_location("handoff_queue",' + repr(new) + ');q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)\n'
            program += 'report=handoff(plan,q,' + repr(envelope['audit']) + ',' + repr(args.digest) + ',timeout=' + str(args.timeout) + ')\n'
            program += 'print(json.dumps({"status":report["status"],"providers":{s:{k:v for k,v in x.items() if k!="queue"} for s,x in report["providers"].items()}}))'
            print(client.run(env + shlex.quote(settings.hpc_runtime_python) + ' -B -c ' + shlex.quote(program), timeout=args.timeout + 90))




def fence_existing_handoff(plan, queue_module, audit, expected_digest, timeout=1800):
    """Hold the old global slot after the current curl exits, for this audit only.

    The active transfer inherits the slot, so this never interrupts its file.
    This companion fences the next legacy transfer while the original handoff
    controller verifies the source-specific gates. It never sends signals.
    """
    if plan.get('digest') != expected_digest or digest({k:v for k,v in plan.items() if k!='digest'}) != expected_digest:
        raise RuntimeError('fence digest mismatch')
    audit = Path(audit)
    raw = json.loads(audit.read_text())
    if raw.get('plan_digest') != expected_digest or raw.get('status') not in {'waiting_file_boundary','stopping_idle_worker'}:
        raise RuntimeError('original handoff is not waiting; inspect only')
    # Locate the exact active operator controller, not shell command strings.
    controllers = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name)==os.getpid():
            continue
        try:
            args = (proc/'cmdline').read_bytes().decode().strip('\0').split('\0')
            if proc.stat().st_uid!=os.getuid() or len(args)!=4 or args[1:3]!=['-B','-c']:
                continue
            if any(line.startswith('report=handoff(plan,q,'+repr(str(audit))+',') for line in args[3].splitlines()):
                controllers.append(identity(int(proc.name)))
        except (FileNotFoundError,ProcessLookupError):
            continue
    if len(controllers)!=1:
        raise RuntimeError('cannot bind the original handoff controller')
    controller=controllers[0]
    path=audit.with_name(audit.stem+'.fence.json')
    if path.exists() or path.is_symlink():
        raise RuntimeError('existing fence audit; never replay')
    value={'plan_digest':expected_digest,'controller':controller,'status':'waiting_current_file','started_at':time.time()}
    save_audit(path,value)
    def expired(*unused):
        raise TimeoutError('boundary fence expired; no download signal sent')
    previous=signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,timeout)
    try:
        root=Path(plan['workers'][0]['root']).parent
        with queue_module.kernel_guard(root/'.pool-download-slot',blocking=True):
            value.update(status='next_legacy_file_fenced',acquired_at=time.time())
            save_audit(path,value)
            while True:
                current=json.loads(audit.read_text())
                if current.get('plan_digest')!=expected_digest:
                    raise RuntimeError('handoff audit changed')
                if current.get('status')=='needs_review':
                    raise RuntimeError('original handoff needs review')
                if current.get('status')=='new_workers_started':
                    value.update(status='released_after_handoff',finished_at=time.time())
                    save_audit(path,value)
                    return value
                check_identity(controller)
                time.sleep(.1)
    except BaseException as error:
        value.update(status='released_needs_review',error=str(error),finished_at=time.time())
        save_audit(path,value)
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        signal.signal(signal.SIGALRM,previous)

if __name__ == '__main__':
    operator_main()
