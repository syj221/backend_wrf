"""Linux integration of the supervisor using isolated, short-lived toy processes.

WRF_MANAGED_PROCESS_TEST_SCRIPT may select an immutable deployed script. No WRF,
MPI, service, production resource directory, or production task is exercised.
"""
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


LAST_REPORT = {}
TOY = r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
root,tag,role=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
def identity():
    fields=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
    return {'pid':os.getpid(),'start_ticks':fields[19],
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
record=root/(role+'.json');pending=record.with_suffix('.new')
pending.write_text(json.dumps({'process':identity(),'session':os.getsid(0),'tag':tag}));os.replace(pending,record)
if role=='child':
    time.sleep(30)
else:
    with (root/'starts.txt').open('a') as stream:
        stream.write(tag+'\n');stream.flush();os.fsync(stream.fileno())
    child=subprocess.Popen([sys.executable,'-B',__file__,str(root),tag,'child'])
    stopped=False
    def stop(signum,frame):
        global stopped
        stopped=True
    signal.signal(signal.SIGTERM,stop)
    deadline=time.monotonic()+30
    try:
        while not stopped and time.monotonic()<deadline and child.poll() is None:
            time.sleep(0.05)
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
'''


def process_identity(pid):
    fields = Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')',1)[1].split()
    return {'pid':pid,'start_ticks':fields[19],
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def alive(expected):
    try:
        return (process_identity(expected['pid'])==expected and
                Path('/proc/{}/stat'.format(expected['pid'])).read_text().rsplit(')',1)[1].split()[0]!='Z')
    except (FileNotFoundError,ProcessLookupError):
        return False


def wait_for(read, accept, timeout=8):
    deadline = time.monotonic()+timeout
    while True:
        value = read()
        if accept(value): return value
        if time.monotonic()>=deadline: raise AssertionError('Timed out waiting for fixture state: '+repr(value))
        time.sleep(0.05)


def session_members(session):
    members = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit(): continue
        try:
            fields = (path/'stat').read_text().rsplit(')',1)[1].split()
            if int(fields[3])!=session or fields[0]=='Z': continue
            if path.stat().st_uid!=os.getuid(): raise AssertionError('Fixture session contains foreign UID')
            members.append(process_identity(int(path.name)))
        except (FileNotFoundError,ProcessLookupError):
            continue
    return members


def assert_fixture_process(expected, base):
    if not alive(expected): return
    process = Path('/proc')/str(expected['pid'])
    try:
        cwd = (process/'cwd').resolve(strict=True)
        arguments = (process/'cmdline').read_bytes().split(b'\0')
    except (FileNotFoundError,ProcessLookupError):
        if not alive(expected): return
        raise
    if (cwd!=base and base not in cwd.parents) or not any(str(base).encode() in argument for argument in arguments):
        raise AssertionError('Process does not have the fixture cwd and argument binding: '+str(expected['pid']))
    if process.stat().st_uid!=os.getuid() or process_identity(expected['pid'])!=expected:
        raise AssertionError('Fixture process identity changed')


def atomic_state(path, value):
    pending = path.with_suffix('.fixture-new')
    with pending.open('w') as stream:
        json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(pending,path)


def fixture_claim_state(root, token):
    """Refuse cleanup when a detached launch could still be registering."""
    attempt = root/'launches'/token
    path = attempt/'state.json'
    markers = [root/'active-launch.json',root/'service.pid',root/'process.json']
    if not path.exists():
        if attempt.exists() or any(marker.exists() for marker in markers):
            raise AssertionError('Fixture claim has no registered state; preserve directory')
        return None
    state = json.loads(path.read_text())
    if not state.get('process'):
        # Only a prelaunch tombstone without a launch marker proves that no
        # detached execute process was created. A completed precommand cancel
        # still cannot identify the briefly surviving execute process.
        prelaunch_cancel = (state.get('status') in {'failed','cancelled'} and
                            state.get('cancelled') is True and
                            state.get('cancellation_verified') is True and
                            state.get('reason')=='cancelled_before_launch' and
                            not any(marker.exists() for marker in markers))
        if not prelaunch_cancel:
            raise AssertionError('Fixture claim has no registered process identity; preserve directory')
    return state


def run_process_scenario(script, report):
    if sys.platform!='linux': raise unittest.SkipTest('Requires Linux /proc and POSIX sessions')
    script = Path(script).resolve(strict=True)
    before_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    temporary = tempfile.TemporaryDirectory(prefix='wrf-managed-process-linux-')
    # Unknown identities must retain their fixture evidence, never trigger an
    # implicit garbage-collection cleanup while a child could still be alive.
    temporary._finalizer.detach()
    base = Path(temporary.name).resolve()
    base.chmod(0o700)
    resource = base/'isolated-resources';resource.mkdir(mode=0o700)
    roots = [base/'task-a',base/'task-b']
    token,other_token,tag = uuid.uuid4().hex,uuid.uuid4().hex,uuid.uuid4().hex
    toy = base/'toy.py';toy.write_text(TOY)
    command = shlex.join([sys.executable,'-B',str(toy),str(base),tag,'parent'])
    environment = dict(os.environ,WRF_COMPUTE_RESOURCE_ROOT=str(resource),PYTHONDONTWRITEBYTECODE='1')
    report.update(schema='wrf-managed-process-linux-1',script=str(script),script_sha256=before_sha,
                  script_readonly=script.stat().st_mode & 0o222==0,fixture_root=str(base),
                  resource_root=str(resource),attempt=token,toy_tag=tag,
                  real_wrf_or_mpi=False,production_resources_accessed=False,commands=[],checks={},
                  cleanup={'temporary_directory_removed':False,'preserved_for_identity_audit':str(base)})
    observed = {}
    original = None
    expected = None
    state_path = roots[0]/'launches'/token/'state.json'

    def call(action, root=roots[0], attempt=token, expected_code=0):
        arguments = [sys.executable,'-B',str(script),action,str(root),attempt]
        if action=='launch': arguments.append(command)
        process = subprocess.Popen(arguments,cwd=base,env=environment,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
        try:
            stdout,stderr = process.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            # This directly owned CLI cannot execute any business command except
            # the tagged toy. Termination does not target its detached session.
            process.terminate();stdout,stderr=process.communicate(timeout=3)
            raise AssertionError('Fixture CLI timed out: '+action)
        report['commands'].append({'action':action,'root':str(root),'attempt':attempt,
                                   'cli_pid':process.pid,'returncode':process.returncode})
        if expected_code==0 and process.returncode!=0:
            raise AssertionError('Fixture CLI failed '+action+': '+stderr[-1500:])
        if expected_code!=0:
            if process.returncode==0: raise AssertionError('Unsafe fixture operation unexpectedly succeeded')
            return stderr
        return json.loads(stdout)

    def remember(expected):
        assert_fixture_process(expected,base)
        observed[(expected['pid'],expected['start_ticks'],expected['boot_id'])] = expected

    try:
        launched = call('launch')
        running = wait_for(lambda:call('status'),lambda state:state.get('status')=='running')
        if launched.get('process')!=running['process']: raise AssertionError('Launch identity changed')
        remember(running['process'])
        parent = wait_for(lambda:json.loads((base/'parent.json').read_text()) if (base/'parent.json').exists() else {},bool)
        child = wait_for(lambda:json.loads((base/'child.json').read_text()) if (base/'child.json').exists() else {},bool)
        for record in (parent,child):
            if record['tag']!=tag or record['session']!=running['process']['pid']:
                raise AssertionError('Toy parent/child session binding differs')
            remember(record['process'])
        members = session_members(running['process']['pid'])
        for member in members: remember(member)
        report['initial_session_members'] = members
        repeated = call('launch')
        if repeated.get('process')!=running['process'] or (base/'starts.txt').read_text().splitlines()!=[tag]:
            raise AssertionError('Duplicate token started another toy process')
        report['checks']['same_token_started_once'] = True
        report['duplicate_launch_process'] = repeated['process']
        denied = call('launch',roots[1],other_token,expected_code=1)
        if 'resource is occupied or unknown' not in denied or (roots[1]/'launches').exists():
            raise AssertionError('Second root was not rejected by the isolated resource lease')
        lease = json.loads((resource/'compute-lease.json').read_text())
        if lease!={'root':str(roots[0]),'attempt':token}: raise AssertionError('Rejected launch changed resource lease')
        report['checks']['second_root_resource_lease_refused'] = True
        report['resource_lease_rejection'] = denied[-1000:]
        reconnected = call('status')
        if reconnected.get('process')!=running['process'] or reconnected.get('status')!='running':
            raise AssertionError('Independent status process did not reconnect')
        report['checks']['independent_status_reconnected'] = True
        report['reconnected_process'] = reconnected['process']
        original = json.loads(state_path.read_text())
        corrupted = json.loads(json.dumps(original))
        corrupted['process']['start_ticks'] = str(int(corrupted['process']['start_ticks'])+1)
        atomic_state(state_path,corrupted)
        unknown = call('status')
        if unknown.get('status')!='unknown': raise AssertionError('Wrong start ticks were accepted')
        denied = call('cancel',expected_code=1)
        if 'identity is unknown; cancellation refused' not in denied or (state_path.parent/'cancellation.json').exists():
            raise AssertionError('Cancellation did not refuse the corrupted identity')
        if not all(alive(record['process']) for record in (parent,child)):
            raise AssertionError('Refused cancellation affected toy parent/child')
        report['checks']['wrong_start_ticks_unknown_and_cancel_refused'] = True
        report['tampered_identity_status'] = unknown['status']
        report['tampered_identity_cancellation_rejection'] = denied[-1000:]
        atomic_state(state_path,original);original=None
        if call('status').get('process')!=running['process']: raise AssertionError('Original fixture state did not restore')
        pending = call('cancel')
        if pending.get('status')!='cancel_pending': raise AssertionError('Cancellation did not persist pending state')
        report['initial_cancellation_status'] = pending['status']
        terminal = wait_for(lambda:call('status'),lambda state:state.get('status')=='cancelled')
        request = json.loads((state_path.parent/'cancellation.json').read_text())
        if terminal.get('cancellation_verified') is not True or request.get('status')!='complete':
            raise AssertionError('Terminal cancellation lacks complete evidence')
        for member in request.get('authorized_members',[]): remember(member)
        if request.get('process'): remember(request['process'])
        wait_for(lambda:session_members(running['process']['pid']),lambda remaining:not remaining)
        wait_for(lambda:[record for record in observed.values() if alive(record)],lambda remaining:not remaining)
        if (base/'starts.txt').read_text().splitlines()!=[tag]: raise AssertionError('Toy start count changed')
        report['checks']['cancel_pending_to_verified_cancelled'] = True
        report['checks']['entire_fixture_session_exited'] = True
        report['terminal_state'] = {key:value for key,value in terminal.items() if key!='log'}
        report['cancel_request'] = request
        report['toy_start_count'] = 1
    finally:
        if original is not None and state_path.exists(): atomic_state(state_path,original)
        state = fixture_claim_state(roots[0],token)
        if state is not None:
            expected = state.get('process')
            if expected:
                remember(expected)
                for member in session_members(expected['pid']): remember(member)
                if alive(expected):
                    with suppress(AssertionError): call('cancel')
            cancellation = state_path.parent/'cancellation.json'
            if cancellation.exists():
                request = json.loads(cancellation.read_text())
                if request.get('process'): remember(request['process'])
                for member in request.get('authorized_members',[]): remember(member)
        # Only identities captured under this fixture's cwd and argv binding may
        # receive the fallback TERM. No signal is sent on identity uncertainty.
        remaining = [record for record in observed.values() if alive(record)]
        for record in remaining:
            assert_fixture_process(record,base)
            if alive(record):
                with suppress(ProcessLookupError): os.kill(record['pid'],signal.SIGTERM)
        try:
            wait_for(lambda:[record for record in observed.values() if alive(record)],lambda value:not value,timeout=35)
            remaining_session = session_members(expected['pid']) if state_path.exists() and expected else []
            if remaining_session: raise AssertionError('Fixture session still has live members')
            report['cleanup']['remaining_live_identities'] = []
            report['cleanup']['observed_identities'] = list(observed.values())
            zombies = []
            for record in observed.values():
                try:
                    fields = Path('/proc/{}/stat'.format(record['pid'])).read_text().rsplit(')',1)[1].split()
                    if fields[0]=='Z' and process_identity(record['pid'])==record: zombies.append(record)
                except (FileNotFoundError,ProcessLookupError):
                    pass
            report['cleanup']['exited_zombie_identities'] = zombies
            report['cleanup']['files_before_removal'] = sum(path.is_file() for path in base.rglob('*'))
            report['cleanup']['logical_bytes_before_removal'] = sum(path.stat().st_size for path in base.rglob('*') if path.is_file())
            temporary.cleanup()
            report['cleanup']['temporary_directory_removed'] = not base.exists()
            report['cleanup'].pop('preserved_for_identity_audit',None)
        except BaseException:
            temporary._finalizer.detach()
            report['cleanup']['temporary_directory_removed'] = False
            report['cleanup']['preserved_for_identity_audit'] = str(base)
            raise
        report['script_sha256_after'] = hashlib.sha256(script.read_bytes()).hexdigest()
        if report['script_sha256_after']!=before_sha: raise AssertionError('Published script changed during read-only execution')
    return report


class ManagedProcessCleanupTest(unittest.TestCase):
    def test_unknown_claim_without_identity_retains_fixture(self):
        with tempfile.TemporaryDirectory(prefix='wrf-managed-claim-fixture-') as directory:
            root,token = Path(directory),'a'*32
            attempt = root/'launches'/token
            attempt.mkdir(parents=True)
            (attempt/'state.json').write_text(json.dumps({'status':'unknown','attempt':token}))
            (root/'active-launch.json').write_text(json.dumps({'attempt':token}))
            with self.assertRaisesRegex(AssertionError,'no registered process identity'):
                fixture_claim_state(root,token)
            self.assertTrue((attempt/'state.json').is_file())

    def test_only_verified_prelaunch_tombstone_allows_identity_free_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='wrf-managed-claim-fixture-') as directory:
            root,token = Path(directory),'b'*32
            attempt = root/'launches'/token
            attempt.mkdir(parents=True)
            state = {'status':'failed','attempt':token,'cancelled':True,
                     'cancellation_verified':True,'reason':'cancelled_before_launch'}
            (attempt/'state.json').write_text(json.dumps(state))
            self.assertEqual(fixture_claim_state(root,token),state)
            state['reason'] = 'cancelled_before_command'
            (attempt/'state.json').write_text(json.dumps(state))
            with self.assertRaisesRegex(AssertionError,'no registered process identity'):
                fixture_claim_state(root,token)


@unittest.skipUnless(sys.platform=='linux','Requires Linux /proc and POSIX sessions')
class ManagedProcessLinuxTest(unittest.TestCase):
    def test_isolated_process_lifecycle_and_fail_closed_identities(self):
        global LAST_REPORT
        LAST_REPORT = {}
        script = os.environ.get('WRF_MANAGED_PROCESS_TEST_SCRIPT') or str(Path(__file__).parent/'scripts/managed_process.py')
        run_process_scenario(script,LAST_REPORT)
        self.assertEqual(len(LAST_REPORT['checks']),6)
        self.assertTrue(all(LAST_REPORT['checks'].values()))
        self.assertTrue(LAST_REPORT['cleanup']['temporary_directory_removed'])


if __name__=='__main__':
    unittest.main()
