"""Operator-only pause bridge for the inspected ECMWF 18 -> 19 worker."""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid

import handoff_pool_workers as h


def exited(fd, milliseconds=0):
    poll = select.poll()
    poll.register(fd, select.POLLIN)
    return bool(poll.poll(milliseconds))


def controllers(audit):
    found = []
    prefix = 'report=handoff(plan,q,' + repr(str(audit)) + ','
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            args = (path/'cmdline').read_bytes().decode().strip('\0').split('\0')
            if (len(args) == 4 and args[1:3] == ['-B', '-c']
                    and any(line.startswith(prefix) for line in args[3].splitlines())):
                found.append(h.identity(int(path.name)))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


def predecessor(audit, plan):
    info = audit.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('unsafe predecessor audit')
    raw = audit.read_bytes()
    value = json.loads(raw)
    if (value.get('plan') != plan or value.get('plan_digest') != plan['digest']
            or h.digest({k:v for k,v in plan.items() if k != 'digest'}) != plan['digest']
            or set(value.get('providers', {})) != {'gfs'}
            or value['providers']['gfs'].get('status') != 'new_worker_started'):
        raise RuntimeError('predecessor has unknown or ECMWF actions')
    return value, hashlib.sha256(raw).hexdigest()


def withdraw(audit, plan, receipt):
    """Trigger the existing controller's own timeout handler, never its workers."""
    if receipt.exists() or receipt.is_symlink():
        raise RuntimeError('existing withdrawal receipt; inspect, never replay')
    value, sha = predecessor(audit, plan)
    active = controllers(audit)
    report = {'status':'checking', 'audit':str(audit), 'audit_sha_before':sha,
              'plan_digest':plan['digest'], 'started_at':time.time()}
    h.save_audit(receipt, report)
    if value['status'] == 'waiting_file_boundary':
        if len(active) != 1:
            raise RuntimeError('cannot bind original controller')
        controller = active[0]
        fd = h.pidfd_open(controller['pid'])
        try:
            h.check_identity(controller)
            h.pidfd_signal(fd, signal.SIGSTOP)
            try:
                deadline = time.monotonic()+3
                while time.monotonic() < deadline:
                    state = (Path('/proc')/str(controller['pid'])/'status').read_text()
                    if '\nState:\tT' in state:
                        break
                    time.sleep(.01)
                else:
                    raise RuntimeError('controller did not stop')
                value, _ = predecessor(audit, plan)
                if value['status'] != 'waiting_file_boundary':
                    raise RuntimeError('controller passed original waiting boundary')
                report.update(status='withdraw_requested', controller=controller)
                h.save_audit(receipt, report)
                h.pidfd_signal(fd, signal.SIGALRM)
            finally:
                h.pidfd_signal(fd, signal.SIGCONT)
            if not exited(fd, 10000):
                raise RuntimeError('controller response unknown; no worker action allowed')
        finally:
            os.close(fd)
    elif value['status'] != 'needs_review' or active:
        raise RuntimeError('predecessor not terminal')
    value, sha = predecessor(audit, plan)
    if (value['status'] != 'needs_review' or not str(value.get('error','')).startswith('TimeoutError:')
            or controllers(audit)):
        raise RuntimeError('predecessor withdrawal unknown; inspect only')
    report.update(status='controller_exited_no_ecmwf_action', audit_sha_after=sha, finished_at=time.time())
    h.save_audit(receipt, report)
    return report


def file_identity(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1):
        raise RuntimeError('unsafe partial file')
    return [info.st_dev, info.st_ino]


def inherited_gate(module, path, process):
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    expected = module.LOCK_ROOT/key
    info = expected.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('unsafe inherited gate')
    fds = Path('/proc')/str(process['pid'])/'fd'
    if not any(p.stat().st_ino == info.st_ino and p.stat().st_dev == info.st_dev for p in fds.iterdir()):
        raise RuntimeError('curl does not inherit required gate')
    with module.kernel_guard(path, blocking=False) as guard:
        if guard is not None:
            raise RuntimeError('expected inherited gate is not held')


def prepare(old, new, root, prior, withdrawal):
    receipt = json.loads(withdrawal.read_text())
    value, sha = predecessor(Path(prior['audit']), prior['plan'])
    if (receipt.get('status') != 'controller_exited_no_ecmwf_action'
            or receipt.get('audit_sha_after') != sha or value['status'] != 'needs_review'
            or controllers(Path(prior['audit']))):
        raise RuntimeError('original transaction not resolved')
    plan = h.prepare({'ecmwf':root}, old, new)
    original = next(w for w in prior['plan']['workers'] if w['source']=='ecmwf')
    if plan['workers'] != [original]:
        raise RuntimeError('original ECMWF identity changed')
    plan.pop('digest')
    plan.update(schema='wrf-pool-pause-1', predecessor=prior, predecessor_sha=sha,
                withdrawal=str(withdrawal), withdrawal_sha=hashlib.sha256(withdrawal.read_bytes()).hexdigest())
    plan['digest'] = h.digest(plan)
    return plan


def pause(plan, module, audit, expected):
    if expected != plan.get('digest') or h.digest({k:v for k,v in plan.items() if k!='digest'}) != expected:
        raise RuntimeError('pause digest mismatch')
    if audit.exists() or audit.is_symlink():
        raise RuntimeError('existing pause audit; inspect, never replay')
    worker = plan['workers'][0]
    if len(plan['workers']) != 1 or worker['source'] != 'ecmwf':
        raise RuntimeError('only the bound ECMWF worker is allowed')
    current = prepare(plan['old_module'],plan['new_module'],worker['root'],plan['predecessor'],Path(plan['withdrawal']))
    if current != plan:
        raise RuntimeError('pause bindings changed')
    for key in ('old','new'):
        h.verify_module(plan[key+'_module'],plan[key+'_sha'])
    root = Path(worker['root'])
    report = {'status':'prepared', 'plan':plan, 'started_at':time.time()}
    h.save_audit(audit, report)
    parent = h.pidfd_open(worker['process']['pid'])
    frozen = False
    terminating = False
    try:
        h.check_identity(worker['process'])
        h.pidfd_signal(parent,signal.SIGSTOP)
        frozen = True
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            if '\nState:\tT' in (Path('/proc')/str(worker['process']['pid'])/'status').read_text():break
            time.sleep(.01)
        else:raise RuntimeError('worker did not stop')
        h.check_identity(worker['process'])
        if h.marker(root) != worker['marker']:
            raise RuntimeError('worker marker changed')
        with module.exclusive(root/'.pool-queue.lock',blocking=False) as state_guard:
            if state_guard is None:raise RuntimeError('worker froze inside state update; resume untouched')
            state = json.loads((root/'.pool-queue.json').read_text())
            active = [(c,k,e) for c,v in state['cycles'].items() for k,e in v['files'].items() if e.get('status')=='downloading']
            if state['source']!='ecmwf' or len(active)!=1:raise RuntimeError('not one active ECMWF file')
            cycle,key,entry = active[0];module.safe_cycle(cycle)
            args = argparse.Namespace(source='ecmwf',root=str(root));pool=module.PoolQueue(args)
            name=pool.file_name(int(key));part=root/cycle/(name+'.part')
            if entry.get('current_file')!=name or part.parent.is_symlink() or part.parent.resolve()!=root/cycle:
                raise RuntimeError('active path changed')
            before=file_identity(part);size=part.stat().st_size
            kids=h.children(worker['process']['pid'])
            if len(kids)!=1:raise RuntimeError('unknown worker children')
            child=h.identity(next(iter(kids)));argv=child['argv']
            expected_url='https://data.ecmwf.int/forecasts/'+cycle[:8]+'/00z/ifs/0p25/oper/'+cycle+'0000-'+str(int(key))+'h-oper-fc.grib2'
            if (child['uid']!=os.getuid() or Path(child['exe']).name!='curl' or argv[-1]!=expected_url
                    or argv[argv.index('--output')+1]!=str(part)
                    or argv[argv.index('--continue-at')+1]!='-' or '--remove-on-error' in argv):
                raise RuntimeError('curl identity or continuation scope changed')
            for gate in (root/'.pool-worker.lockdir', root/'.pool-download-slot'):
                inherited_gate(module,gate,child)
            inherited_gate(module,pool.cycle_lock(cycle),worker['process'])
            curl=h.pidfd_open(child['pid'])
            try:
                h.check_identity(child)
                attempt=uuid.uuid4().hex
                # This is an observed interruption, not reconstructed legacy connection history.
                pool.record_attempt(cycle,{'event':'start','id':attempt,'file':name,'attempt':entry.get('attempts',0),
                    'started_at':module.utc_now(),'bytes_before':size,'origin':'operator_pause_observation'})
                report.update(status='interrupt_requested',child=child,partial=str(part),partial_identity=before,
                              bytes_before=size,attempt_id=attempt,queue_sha=h.digest(state))
                h.save_audit(audit,report)
                terminating=True
                h.pidfd_signal(curl,signal.SIGTERM)
                if not exited(curl,15000):raise RuntimeError('curl did not exit; no force kill or restart')
                if file_identity(part)!=before or part.stat().st_size<size:raise RuntimeError('partial changed unexpectedly')
                fd=os.open(part,os.O_RDONLY|os.O_NOFOLLOW)
                try:os.fsync(fd)
                finally:os.close(fd)
                report.update(status='curl_exited',bytes_after=part.stat().st_size)
                h.save_audit(audit,report)
                h.pidfd_signal(parent,signal.SIGTERM)
                h.pidfd_signal(parent,signal.SIGCONT);frozen=False
                if not exited(parent,15000):raise RuntimeError('worker did not exit; no force kill or restart')
                pool.record_attempt(cycle,{'event':'finish','id':attempt,'status':'interrupted','cause':'operator_pause',
                    'finished_at':module.utc_now(),'bytes_after':part.stat().st_size})
            finally:os.close(curl)
        # All inherited kernel gates must be free before any new worker is spawned.
        with contextlib.ExitStack() as stack:
            for gate in (root/'.pool-worker.lockdir',root/'.pool-download-slot',pool.cycle_lock(cycle)):
                if stack.enter_context(module.kernel_guard(gate,blocking=False)) is None:
                    raise RuntimeError('old gate still held; no restart')
            after=json.loads((root/'.pool-queue.json').read_text())
            if h.digest(after)!=report['queue_sha'] or file_identity(part)!=before:
                raise RuntimeError('queue or partial changed after pause')
            report.update(status='old_exited_gates_released',released_at=time.time())
            h.save_audit(audit,report)
        argv=list(worker['process']['argv']);argv[0]=worker['process']['exe'];argv[1]=plan['new_module']
        log=root/'logs/pool_worker.out'
        if log.parent.is_symlink() or log.parent.resolve()!=root/'logs':raise RuntimeError('unsafe worker log')
        fd=os.open(log,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise RuntimeError('unsafe worker log')
            # Record uncertainty before Popen: a lost response must never replay this operation.
            report.update(status='starting_new_worker');h.save_audit(audit,report)
            proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=fd,stderr=fd,close_fds=True,start_new_session=True)
        finally:os.close(fd)
        report.update(status='new_worker_started',spawned_pid=proc.pid,finished_at=time.time());h.save_audit(audit,report)
        return report
    except BaseException as error:
        report.update(status='needs_review',error=type(error).__name__+': '+str(error),finished_at=time.time())
        h.save_audit(audit,report)
        raise
    finally:
        if frozen and not terminating:
            h.pidfd_signal(parent,signal.SIGCONT)
        os.close(parent)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['withdraw','plan','apply'])
    p.add_argument('--previous-plan',type=Path,required=True)
    p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--digest')
    a=p.parse_args();project=Path(__file__).resolve().parents[2]
    if project/'.run' not in a.plan.resolve().parents:raise RuntimeError('plan must be inside workspace .run')
    sys.path.insert(0,str(project/'backend_wrf'))
    from config import settings
    from hpc_transport import HpcClient
    from runtime_release import require_ready
    if settings.hpc_runtime_release!='ordinary-20261020-19':raise RuntimeError('unexpected release bridge')
    prior=json.loads(a.previous_plan.read_text());base=settings.hpc_release_root+'/maintenance/pool-handoff/'
    if prior['audit']!=base+prior['plan']['digest']+'.json':raise RuntimeError('unexpected original audit')
    withdrawal=base+prior['plan']['digest']+'.withdraw.json'
    header=Path(h.__file__).read_text().split("\nif __name__ == '__main__':",1)[0]
    own=Path(__file__).read_text().split("\nif __name__ == '__main__':",1)[0].replace('import handoff_pool_workers as h','import types\nh=types.SimpleNamespace(**globals())',1)
    program=header+'\n'+own+'\nprior=json.loads('+repr(json.dumps(prior))+')\n'
    program+='withdrawal=Path('+repr(withdrawal)+')\n'
    if a.action=='withdraw':
        # This action only ends the original controller; it cannot acquire the lock it holds locally.
        program+='print(json.dumps(withdraw(Path(prior["audit"]),prior["plan"],withdrawal)))\n'
        result=json.loads(HpcClient(settings).run(shlex.quote(settings.hpc_runtime_python)+' -B -c '+shlex.quote(program),timeout=40))
        print(json.dumps({k:v for k,v in result.items() if k!='controller'}));return
    import fcntl
    with (project/'.run/wrf-maintenance.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        client=HpcClient(settings);ready=require_ready(client,settings,force=True)
        module=prior['plan']['new_module']
        program+='spec=importlib.util.spec_from_file_location("pause_queue",'+repr(module)+');q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)\n'
        if a.action=='plan':
            program+='print(json.dumps(prepare(prior["plan"]["old_module"],prior["plan"]["new_module"],'+repr(settings.hpc_ec_dir)+',prior,withdrawal)))\n'
            plan=json.loads(client.run(shlex.quote(settings.hpc_runtime_python)+' -B -c '+shlex.quote(program),timeout=30))
            envelope={'plan':plan,'fingerprint':ready['fingerprint'],'audit':base+plan['digest']+'.pause.json'}
            with a.plan.open('x') as stream:json.dump(envelope,stream,indent=2);stream.flush();os.fsync(stream.fileno())
            print(json.dumps({'status':'planned','digest':plan['digest']}));return
        envelope=json.loads(a.plan.read_text());plan=envelope['plan']
        if a.digest!=plan['digest'] or envelope['fingerprint']!=ready['fingerprint'] or envelope['audit']!=base+plan['digest']+'.pause.json':
            raise RuntimeError('approved pause binding changed')
        program+='print(json.dumps(pause(json.loads('+repr(json.dumps(plan))+'),q,Path('+repr(envelope['audit'])+'),'+repr(a.digest)+')))\n'
        report=json.loads(client.run(shlex.quote(settings.hpc_runtime_python)+' -B -c '+shlex.quote(program),timeout=90))
        a.plan.with_suffix('.result.json').write_text(json.dumps(report,indent=2));print(json.dumps({k:v for k,v in report.items() if k!='plan'}))


if __name__ == '__main__':
    main()
