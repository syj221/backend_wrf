"""End one identity-bound stopped obsolete prefetch; never spawn or delete data.

The operator must hold the API-host reference guard, check local task/download
references, and explicitly retain this cycle in the authenticated pool policy.
Remote references and inherited process locks are checked again here.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import time
import uuid

import handoff_pool_workers as h
from pause_pool_worker import exited, file_identity, inherited_gate


def stopped(pid):
    return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0] == 'T'


def inventory(path):
    if path.is_symlink() or path.resolve() != path or path.stat().st_uid != os.getuid():
        raise RuntimeError('cycle directory ownership changed')
    return {p.name: [*file_identity(p), p.stat().st_size] for p in path.iterdir()}


def unreferenced(item):
    files = item.get('files') or {}
    if not files or any(e.get('references') or e.get('legacy_task_reference')
                        or e.get('priority') == 'task' or e.get('prefetch_reference') for e in files.values()):
        raise RuntimeError('cycle still has task or prefetch references')


def prepare(module, raw_root, cycle):
    module.safe_cycle(cycle)
    root = Path(raw_root).resolve(strict=True)
    if root.name not in {'gfs', 'ecmwf'}:
        raise RuntimeError('only obsolete GFS/ECMWF 0.25 prefetch may be retired')
    # Reuse the existing command, owner, marker, binary and boot identity binding.
    plan = h.prepare({root.name: str(root)}, module.__file__, module.__file__)
    worker = plan['workers'][0]
    if not stopped(worker['process']['pid']):
        raise RuntimeError('worker is not stopped; no active download may be interrupted')
    pool = module.PoolQueue(argparse.Namespace(source=root.name, root=str(root)))
    state = pool.load_state()
    if state.get('source') != root.name:
        raise RuntimeError('source ownership mismatch')
    item = state.get('cycles', {}).get(cycle, {})
    unreferenced(item)
    active = [(c, key, e) for c, row in state.get('cycles', {}).items()
              for key, e in row.get('files', {}).items() if e.get('status') == 'downloading']
    if len(active) != 1 or active[0][0] != cycle:
        raise RuntimeError('not one bound obsolete transfer')
    key, entry = active[0][1:]
    name = pool.file_name(int(key))
    part = root / cycle / (name + '.part')
    kids = h.children(worker['process']['pid'])
    if len(kids) != 1 or entry.get('current_file') != name:
        raise RuntimeError('transfer scope changed')
    child = h.identity(next(iter(kids)))
    argv = child['argv']
    if (child['uid'] != os.getuid() or Path(child['exe']).name != 'curl'
            or not stopped(child['pid']) or argv.count('--output') != 1
            or Path(argv[argv.index('--output') + 1]).resolve() != part
            or argv[argv.index('--continue-at') + 1] != '-' or '--remove-on-error' in argv):
        raise RuntimeError('stopped resumable curl identity mismatch')
    for gate in (root / '.pool-worker.lockdir', root / '.pool-download-slot'):
        inherited_gate(module, gate, child)
    inherited_gate(module, pool.cycle_lock(cycle), worker['process'])
    plan.pop('digest')
    plan.update(schema='wrf-obsolete-prefetch-stop-1', cycle=cycle, key=key,
                child=child, item_sha=h.digest(item), files=inventory(root / cycle))
    plan['digest'] = h.digest(plan)
    return plan


def retire(plan, module, audit, expected):
    if expected != plan.get('digest') or h.digest({k: v for k, v in plan.items() if k != 'digest'}) != expected:
        raise RuntimeError('plan digest changed')
    if audit.exists() or audit.is_symlink():
        raise RuntimeError('existing operation; inspect, never replay')
    worker = plan['workers'][0]
    root, cycle = Path(worker['root']), plan['cycle']
    pool = module.PoolQueue(argparse.Namespace(source=root.name, root=str(root)))
    report = {'status': 'checking', 'plan': plan, 'started_at': time.time()}
    # An exact manifest is saved before any signals; an uncertain write is not replayable.
    h.save_audit(audit, report)
    try:
        with module.exclusive(pool.state_lock, blocking=False) as guard:
            if guard is None:
                raise RuntimeError('queue busy; untouched')
            if prepare(module, str(root), cycle) != plan:
                raise RuntimeError('process, reference, or file bindings changed; untouched')
            parent, child = h.pidfd_open(worker['process']['pid']), h.pidfd_open(plan['child']['pid'])
            try:
                h.check_identity(worker['process']); h.check_identity(plan['child'])
                event_id = uuid.uuid4().hex
                part = root / cycle / (pool.file_name(int(plan['key'])) + '.part')
                size = part.stat().st_size
                pool.record_attempt(cycle, {'event': 'start', 'id': event_id, 'file': part.name[:-5],
                    'started_at': module.utc_now(), 'bytes_before': size, 'origin': 'operator_stop_observation'})
                report.update(status='stopping_bound_transfer', event_id=event_id)
                h.save_audit(audit, report)
                # SIGTERM is pending before SIGCONT: stopped curl exits without
                # being resumed to download another block or changing its scope.
                h.pidfd_signal(child, signal.SIGTERM)
                h.pidfd_signal(child, signal.SIGCONT)
                if not exited(child, 15000):
                    raise RuntimeError('curl exit unknown; no worker restart')
                if inventory(root / cycle) != plan['files']:
                    raise RuntimeError('files changed after curl exit; inspect only')
                fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW)
                try: os.fsync(fd)
                finally: os.close(fd)
                report.update(status='transfer_exited'); h.save_audit(audit, report)
                h.pidfd_signal(parent, signal.SIGTERM)
                h.pidfd_signal(parent, signal.SIGCONT)
                if not exited(parent, 15000):
                    raise RuntimeError('worker exit unknown; no worker restart')
            finally:
                os.close(child); os.close(parent)
            with contextlib.ExitStack() as stack:
                for gate in (root / '.pool-worker.lockdir', root / '.pool-download-slot', pool.cycle_lock(cycle)):
                    if stack.enter_context(module.kernel_guard(gate, blocking=False)) is None:
                        raise RuntimeError('inherited lock remains; no queue changes or restart')
                state = pool.load_state()
                item = state['cycles'][cycle]
                unreferenced(item)
                if h.digest(item) != plan['item_sha'] or inventory(root / cycle) != plan['files']:
                    raise RuntimeError('state changed after exit; inspect only')
                # Finish the existing attempt when identifiable, and retain a
                # separate operator observation rather than inventing old history.
                for row in pool.attempt_history(cycle)['items']:
                    if row.get('id') != event_id and row.get('status') == 'unfinished' and row.get('file') == part.name[:-5]:
                        pool.record_attempt(cycle, {'event': 'finish', 'id': row['id'], 'status': 'interrupted',
                            'cause': 'obsolete_prefetch_stopped', 'finished_at': module.utc_now(), 'bytes_after': size})
                pool.record_attempt(cycle, {'event': 'finish', 'id': event_id, 'status': 'interrupted',
                    'cause': 'obsolete_prefetch_stopped', 'finished_at': module.utc_now(), 'bytes_after': size})
                for entry in item['files'].values():
                    if entry.get('status') != 'ready':
                        entry.update(status='cancelled', current_file=None, retry_at=0, download_rate_bps=0)
                item.update(publication='expired', active_file=None, updated_at=module.utc_now())
                pool.save_state(state)
                report.update(status='complete', files_after=inventory(root / cycle), finished_at=time.time())
                h.save_audit(audit, report)
        return report
    except BaseException as error:
        report.update(status='needs_review', error=type(error).__name__ + ': ' + str(error), finished_at=time.time())
        h.save_audit(audit, report)
        raise
