"""Explicit ordinary-WRF generator deployment, independent of task execution."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import sys
import urllib.request
import uuid

FILES = ('namelist_layout.py', 'namelist_source_probe.py', 'apply_namelist_plan.py')
SCRIPT_DIR = Path(__file__).resolve().parent

# Python 3.6-compatible remote transaction. No WPS/WRF binary is executed.
REMOTE = r'''
import fcntl, hashlib, json, os, shutil, sys
from pathlib import Path
root, mode = Path(sys.argv[1]), sys.argv[2]
names = ('namelist_layout.py', 'namelist_source_probe.py', 'apply_namelist_plan.py')
def digest(p):
    if p.is_symlink() or (p.exists() and not p.is_file()):
        raise ValueError('Generator target is not a regular file')
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
if mode == 'inspect':
    print(json.dumps({name: digest(root/name) for name in names}))
else:
    stage, old, new = Path(sys.argv[3]), json.loads(sys.argv[4]), json.loads(sys.argv[5])
    if (stage.parent != root/'.generator-deployments' or stage.is_symlink()
        or len(stage.name) != 32 or any(c not in 'abcdef0123456789' for c in stage.name)
        or set(old) != set(names) or set(new) != set(names)):
        raise ValueError('Invalid generator deployment scope')
    with (root/'.generator-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for name in names:
            if digest(root/name) != old[name] or digest(stage/name) != new[name]:
                raise ValueError('Generator changed concurrently or staging hash mismatch')
            compile((stage/name).read_bytes(), name, 'exec')
        namespace = {}
        exec(compile((stage/'namelist_layout.py').read_bytes(), 'namelist_layout.py', 'exec'), namespace)
        validate = namespace['validate_layout']
        validate('&share\n/\n&geogrid\n/\n&ungrib\n/\n&metgrid\n/\n', 'wps')
        try:
            validate('&geogrid\n/\n&metgrid\n/\n&share\n/\n&ungrib\n/\n', 'wps')
        except ValueError:
            pass
        else:
            raise ValueError('Staged layout validator accepted the old invalid order')
        backup = stage/'backup'
        backup.mkdir(mode=0o700)
        for name in names:
            if old[name] is not None:
                shutil.copy2(str(root/name), str(backup/name))
        manifest = {'old': old, 'new': new, 'status': 'prepared'}
        audit = stage/'deployment.json'
        def save():
            with audit.open('w') as out:
                json.dump(manifest, out); out.flush(); os.fsync(out.fileno())
        save()
        changed = []
        try:
            # Install dependencies before the importing writer.
            for name in names:
                os.replace(str(stage/name), str(root/name)); changed.append(name)
            if any(digest(root/name) != new[name] for name in names):
                raise ValueError('Installed generator hash mismatch')
            manifest['status'] = 'complete'; save()
        except Exception:
            for name in reversed(changed):
                if digest(root/name) != new[name]:
                    raise ValueError('Concurrent change: backup retained, manual reconciliation required')
                if old[name] is None:
                    (root/name).unlink()
                else:
                    shutil.copy2(str(backup/name), str(root/name))
            manifest['status'] = 'rolled_back'; save()
            raise
        print(json.dumps({'status': 'complete', 'deployment': stage.name,
                          'hashes': {name: digest(root/name) for name in names}}))
'''


def ordinary_idle(settings):
    with closing(sqlite3.connect(settings.database_path.as_uri()+'?mode=ro', uri=True, timeout=3)) as db:
        states = [row[0] for row in db.execute('SELECT status FROM wrf_tasks')]
    if any(state not in {'succeeded', 'partial_success', 'failed', 'waiting_restart', 'cancelled'} for state in states):
        raise RuntimeError('Ordinary task is active or uncertain; deployment refused')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8007/api/health/activity', timeout=5) as response:
        envelope = json.load(response)
    data = envelope.get('data', {})
    if (envelope.get('code') != 0 or data.get('schema') != 'wrf-local-activity-1'
            or type(data.get('active_task_count')) is not int or data['active_task_count'] != 0
            or type(data.get('queue_size')) is not int or data['queue_size'] != 0):
        raise RuntimeError('Ordinary queue cannot be verified idle; deployment refused')


def remote_command(root, mode, *args):
    return 'python3 -B -c '+shlex.quote(REMOTE)+' '+ ' '.join(shlex.quote(str(a)) for a in (root, mode, *args))


def deploy(client, settings, apply=False):
    from namelist_config import source_catalog
    ordinary_idle(settings)
    root = client.service_dir()
    source = source_catalog()
    probe = (SCRIPT_DIR/'namelist_source_probe.py').read_text()
    evidence = json.loads(client.run('python3 -B -c '+shlex.quote(probe)+' '+
        ' '.join(shlex.quote(p) for p in (settings.hpc_wrf_source_dir, settings.hpc_wps_source_dir, root)), timeout=20))
    if any(evidence.get(key) != source[key] for key in ('source_hash', 'binary_hashes')):
        raise RuntimeError('WRF/WPS build mismatch; not a generator-only repair')
    wrf_hash = hashlib.sha256((SCRIPT_DIR/'wrf.sh').read_bytes()).hexdigest()
    if evidence.get('generator_hashes', {}).get('wrf.sh') != wrf_hash:
        raise RuntimeError('wrf.sh differs; deployment scope must be reviewed separately')
    old = json.loads(client.run(remote_command(root, 'inspect'), timeout=15))
    new = {name: hashlib.sha256((SCRIPT_DIR/name).read_bytes()).hexdigest() for name in FILES}
    if not apply:
        return {'mode': 'check', 'matches': {name: old.get(name) == new[name] for name in FILES}}
    if old == new:
        return {'status': 'already_current', 'hashes': new}
    token = uuid.uuid4().hex
    stage = root+'/.generator-deployments/'+token
    client.run('mkdir -p -- '+shlex.quote(root+'/.generator-deployments')+
        ' && chmod 700 -- '+shlex.quote(root+'/.generator-deployments')+
        ' && mkdir -m 700 -- '+shlex.quote(stage), timeout=15)
    for name in FILES:
        client.upload(SCRIPT_DIR/name, stage+'/'+name, timeout=30)
    ordinary_idle(settings)
    # A lost response is not retried; the retained deployment audit is authoritative.
    return json.loads(client.run(remote_command(root, 'install', stage,
        json.dumps(old), json.dumps(new)), timeout=20))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Back up and deploy only the three Python generator modules.')
    args = parser.parse_args()
    sys.path.insert(0, str(SCRIPT_DIR.parent))
    from config import settings
    from hpc_transport import HpcClient
    print(json.dumps(deploy(HpcClient(settings), settings, args.apply), ensure_ascii=False))
