"""Read-only terminal proofs shared by maintenance and task management."""
import json
import re
import shlex
import sqlite3
from pathlib import Path
TOKEN = re.compile(r"[0-9a-f]{32}")

WAITING_PROBE = r'''
import fcntl,hashlib,json,os,stat,sys
from pathlib import Path
mode,control,runtime=sys.argv[1],Path(sys.argv[2]),json.loads(sys.argv[3])
def read_regular(path):
    descriptor=os.open(str(path),os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>1048576:raise ValueError('Expected bounded regular terminal control file')
        value=stream.read(1048577)
        if len(value)>1048576:raise ValueError('Terminal control file grew beyond bound')
        return value.strip()
def no_live_session(pid):
    live=[]
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:fields=(path/'stat').read_text().rsplit(')',1)[1].split()
        except FileNotFoundError:continue
        except PermissionError as exc:raise ValueError('Cannot prove old session terminal: unreadable /proc/'+path.name+'/stat') from exc
        if fields[0]!='Z' and (int(path.name)==pid or int(fields[3])==pid):live.append(int(path.name))
    if live:raise ValueError('Waiting-restart task still has live or identity-uncertain processes')
def no_task_processes():
    observer_session=os.getsid(0)
    needles=[str(control.resolve().parent).encode(),control.parent.name.encode()]
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            fields=(path/'stat').read_text().rsplit(')',1)[1].split()
            if fields[0]=='Z' or int(fields[3])==observer_session or path.stat().st_uid!=os.getuid():continue
            command=(path/'cmdline').read_bytes()
        except FileNotFoundError:continue
        except PermissionError as exc:raise ValueError('Owned process identity is unreadable') from exc
        if any(needle and needle in command for needle in needles):
            raise ValueError('Task has an unaccounted live process outside the recorded session')
def cancelled_claim(state):
    return (state.get('cancelled') is True and state.get('cancellation_verified') is True
            and state.get('reason') in ('cancelled_before_launch','cancelled_before_command')
            and state.get('status')=='failed' and state.get('process') is None)
def prior_terminal(path,expected):
    value=json.loads(read_regular(path/'state.json'))
    if value.get('attempt')!=path.name or expected.get('launch_attempt')!=path.name:
        raise ValueError('Historical managed attempt token mismatch')
    if (not Path(expected.get('remote_task_dir') or '').is_absolute()
            or Path(expected['remote_task_dir']).resolve()!=control.resolve()):
        raise ValueError('Historical control directory is not bound to this task')
    status='cancelled' if value.get('cancelled') and value.get('cancellation_verified') else value.get('status')
    if status not in ('failed','succeeded','cancelled') or status!=expected.get('remote_terminal_status'):
        raise ValueError('Historical attempt is running, unknown, or terminal evidence conflicts')
    process=value.get('process')
    if process!=expected.get('remote_process'):
        raise ValueError('Historical attempt process differs from persisted identity')
    cancellation=path/'cancellation.json'
    if os.path.lexists(str(cancellation)) and json.loads(read_regular(cancellation)).get('status')!='complete':
        raise ValueError('Historical cancellation remains pending')
    if process is None:
        if not cancelled_claim(value):raise ValueError('Historical attempt has no terminal process identity')
    else:
        if (type(process.get('pid')) is not int or process['pid']<=1
                or not str(process.get('start_ticks','')).isdigit() or int(process['start_ticks'])<=0
                or not __import__('re').fullmatch('[a-f0-9-]{36}',str(process.get('boot_id','')))
                or expected.get('remote_pid') not in (None,process['pid'])):
            raise ValueError('Historical process identity is incomplete')
        if status in ('failed','succeeded'):
            code=value.get('exit_code')
            if type(code) is not int or (code==0)!=(status=='succeeded'):
                raise ValueError('Historical exit evidence is incomplete')
        no_live_session(process['pid'])
    return value
if mode=='not_launched':
    if os.path.lexists(str(control)) or os.path.lexists(str(control.parent/'attempts')):raise ValueError('Unlaunched task has remote control or attempt artifacts')
    result={'mode':mode,'terminal':True,'control_absent':True,'attempts_absent':True}
elif mode=='managed':
    lock=control/'launch.lock';lock_held=False
    if os.path.lexists(str(lock)):
        descriptor=os.open(str(lock),os.O_RDONLY|os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):raise ValueError('Invalid managed launch lock')
        try:fcntl.flock(descriptor,fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Managed launch or cancellation is still busy')
        lock_held=True
    token=runtime['launch_attempt'];path=control/'launches'/token/'state.json'
    for directory in (control/'launches',path.parent):
        if not stat.S_ISDIR(directory.lstat().st_mode):raise ValueError('Managed launch directory is not a real directory')
    state=json.loads(read_regular(path))
    expected=runtime.get('remote_process')
    if state.get('attempt')!=token:raise ValueError('Waiting-restart managed attempt token mismatch')
    if not expected and state.get('process') is None:
        if (not state.get('cancelled') or not state.get('cancellation_verified') or
                state.get('reason') not in ('cancelled_before_launch','cancelled_before_command') or
                state.get('status')!='failed'):
            raise ValueError('Managed attempt has no verified prelaunch cancellation')
        markers=('service.pid','process.json','exit.code')
        present=[name for name in markers if os.path.lexists(str(control/name))]
        active=control/'active-launch.json'
        active_token=json.loads(read_regular(active)).get('attempt') if os.path.lexists(str(active)) else None
        historical=runtime.get('_terminal_prior_attempts',[])
        bound={item['launch_attempt']:item for item in historical}
        if len(bound)!=len(historical):raise ValueError('Duplicate historical launch binding')
        verified={}
        if present and (len(present)!=len(markers) or not lock_held):
            raise ValueError('Prelaunch cancellation conflicts with launch evidence')
        for previous in (control/'launches').iterdir():
            if previous.name==token:continue
            if (not __import__('re').fullmatch('[a-f0-9]{32}',previous.name)
                    or not stat.S_ISDIR(previous.lstat().st_mode) or previous.name not in bound):
                raise ValueError('Prelaunch cancellation conflicts with launch evidence: unbound historical attempt')
            verified[previous.name]=prior_terminal(previous,bound[previous.name])
        if set(bound)!=set(verified):raise ValueError('Historical attempt terminal evidence is missing')
        if active_token not in (None,token) and active_token not in verified:
            raise ValueError('Another managed attempt exists')
        if present:
            prior=verified.get(active_token)
            if (not prior or active_token==token or not prior.get('process')
                    or json.loads(read_regular(control/'process.json'))!=prior['process']
                    or read_regular(control/'service.pid')!=str(prior['process']['pid'])
                    or read_regular(control/'exit.code')!=str(prior.get('exit_code'))):
                raise ValueError('Prelaunch cancellation conflicts with launch evidence')
        cancellation=path.parent/'cancellation.json'
        if os.path.lexists(str(cancellation)) and json.loads(read_regular(cancellation)).get('status')!='complete':
            raise ValueError('Prelaunch cancellation remains pending')
        no_task_processes()
        print(json.dumps({'mode':mode,'terminal':True,'never_started':True,'state_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'historical_terminal_attempts':[{'attempt':key,'status':value['status'],'process':value.get('process'),'exit_code':value.get('exit_code')} for key,value in sorted(verified.items())]}))
        sys.exit(0)
    if state.get('process')!=expected:raise ValueError('Waiting-restart managed attempt identity mismatch')
    cancellation=path.parent/'cancellation.json'
    if os.path.lexists(str(cancellation)) and json.loads(read_regular(cancellation)).get('status')!='complete':raise ValueError('Waiting-restart cancellation remains pending')
    status=state.get('status')
    if status=='failed':
        if int(state.get('exit_code',0))==0:raise ValueError('Missing failed managed exit code')
    elif status=='cancelled':
        if not state.get('cancelled') or not state.get('cancellation_verified'):raise ValueError('Unverified managed cancellation')
    else:raise ValueError('Waiting-restart managed attempt is active or uncertain')
    no_live_session(int(expected['pid']))
    result={'mode':mode,'terminal':True,'state':{k:state.get(k) for k in ('status','exit_code','attempt','process','cancellation_verified')},'state_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
else:
    code=read_regular(control/'exit.code');pid=read_regular(control/'service.pid')
    if not code.lstrip('-').isdigit() or int(code)==0 or not pid.isdigit() or int(pid)!=int(runtime['remote_pid']):raise ValueError('Legacy failed exit/PID evidence mismatch')
    no_live_session(int(pid))
    result={'mode':'legacy_failed','terminal':True,'exit_code':int(code),'pid':int(pid),'exit_sha256':hashlib.sha256((control/'exit.code').read_bytes()).hexdigest(),'pid_sha256':hashlib.sha256((control/'service.pid').read_bytes()).hexdigest()}
print(json.dumps(result))
'''


def terminal_mode(task):
    """Validate persisted inputs before any remote asset scan."""
    runtime = task["runtime"]
    token = runtime.get("launch_attempt")
    if token:
        prelaunch_cancel = (task.get("status") == "cancelled"
                            and runtime.get("remote_terminal_status") == "cancelled"
                            and runtime.get("remote_process") is None)
        if (not TOKEN.fullmatch(str(token)) or runtime.get("remote_launch_attempted") is not True
                or (not isinstance(runtime.get("remote_process"), dict) and not prelaunch_cancel)):
            raise RuntimeError("Waiting-restart task has an unknown managed launch")
        mode = "managed"
    elif runtime.get("remote_launch_attempted") is True:
        observation = runtime.get("remote_observation") or {}
        if (runtime.get("remote_terminal_status") != "failed" or observation.get("status") != "failed"
                or str(observation.get("exit_code", "")).lstrip("-").isdigit() is False
                or int(observation["exit_code"]) == 0 or type(runtime.get("remote_pid")) is not int):
            raise RuntimeError("Waiting-restart legacy launch has no persisted failed identity")
        mode = "legacy_failed"
    else:
        markers = ("remote_pid", "remote_process", "remote_task_dir", "remote_output_dir", "remote_launch_status")
        cancelled_before_launch = task.get("status") == "cancelled"
        if ((not runtime.get("release_id") and not cancelled_before_launch) or runtime.get("remote_launch_attempted") not in (None, False)
                or any(runtime.get(key) is not None for key in markers)
                or ((task.get("failure") or {}).get("stage") != "checking_hpc_forcing" and not cancelled_before_launch)):
            raise RuntimeError("Waiting-restart task cannot be proven never launched")
        mode = "not_launched"
    return mode


def prior_attempt_bindings(settings, task):
    """Read only this task's older persisted attempt identities; never trust runtime extras."""
    if not isinstance(task.get("attempt_no"),int) or task["attempt_no"]<2:
        return []
    database=getattr(settings,"database_path",None)
    if database is None:
        return []
    with sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro",uri=True,timeout=5) as db:
        row=db.execute("SELECT attempt_no,runtime_json FROM wrf_tasks WHERE id=?",(task["id"],)).fetchone()
        if (not row or row[0]!=task["attempt_no"]
                or json.loads(row[1] or '{}').get('launch_attempt')!=task['runtime'].get('launch_attempt')):
            raise RuntimeError("Persisted current attempt binding changed")
        result=[]
        for number,raw in db.execute("SELECT attempt_no,runtime_json FROM wrf_task_attempts WHERE task_id=? AND attempt_no<? ORDER BY attempt_no",(task["id"],task["attempt_no"])):
            runtime=json.loads(raw or '{}');token=runtime.get('launch_attempt')
            if token and TOKEN.fullmatch(str(token)):
                result.append({"attempt_no":number,**{key:runtime.get(key) for key in ('launch_attempt','remote_pid','remote_process','remote_terminal_status','remote_task_dir')}})
        return result


def verify_waiting_restart(settings, client, task):
    """Allow a waiting task only with fresh terminal or never-launched proof."""
    if client is None:
        raise RuntimeError("Waiting-restart tasks require fresh read-only remote terminal verification")
    runtime = dict(task["runtime"])
    runtime.pop("_terminal_prior_attempts",None)
    mode = terminal_mode(task)
    if mode=="managed" and runtime.get("remote_process") is None:
        runtime["_terminal_prior_attempts"]=prior_attempt_bindings(settings,task)
    args = (mode, client.task_dir(task["id"]), json.dumps(runtime, ensure_ascii=False))
    command = " ".join(shlex.quote(v) for v in (settings.hpc_runtime_python, "-B", "-c", WAITING_PROBE, *args))
    proof = json.loads(client.run(command, timeout=60))
    if proof.get("terminal") is not True or proof.get("mode") != mode:
        raise RuntimeError("Waiting-restart terminal verification returned an invalid proof")
    return {"task_id": task["id"], **proof}


def verify_forcing_terminal(settings, client, task):
    """Prove a terminal consumer before releasing only its forcing reference."""
    runtime = task.get("runtime") or {}
    if runtime.get("remote_wrf_succeeded") or runtime.get("remote_terminal_status") == "succeeded":
        from retirement import remote_call
        observed = remote_call(client,"terminal",[{"control":client.task_dir(task["id"]),
            "attempt":runtime.get("launch_attempt"),"status":"succeeded"}],timeout=30)
        if len(observed) != 1 or observed[0].get("verified") is not True:
            raise RuntimeError("Successful session terminal identity unavailable")
        return {"task_id":task["id"],"terminal":True,**observed[0]}
    return verify_waiting_restart(settings,client,task)

