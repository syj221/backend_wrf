"""Read-only by default; archive one deleted task's proven stale compute lease."""
import argparse
import fcntl
import inspect
import json
from pathlib import Path
import shlex
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compute_lease
from config import settings
from hpc_transport import HpcClient


def approval(db, deletion_id, digest):
    row = db.execute('SELECT task_id,status,data_json FROM wrf_task_deletion_plans WHERE id=?', (deletion_id,)).fetchone()
    if not row or row[1] != 'complete':
        raise ValueError('Deletion is not complete')
    plan = json.loads(row[2]); manifest = plan['manifest']
    tombstone = db.execute('SELECT deletion_plan_id,status FROM wrf_task_tombstones WHERE task_id=?', (row[0],)).fetchone()
    if tombstone != (deletion_id, 'deleted') or manifest['digest'] != digest or manifest['task_id'] != row[0]:
        raise ValueError('Deletion digest or tombstone binding differs')
    if db.execute('SELECT 1 FROM wrf_tasks WHERE status NOT IN (?,?,?,?) LIMIT 1',
                  ('succeeded', 'partial_success', 'failed', 'cancelled')).fetchone():
        raise ValueError('Ordinary task is active or unknown')
    if db.execute('SELECT 1 FROM wrf_tasks WHERE id=?', (row[0],)).fetchone():
        raise ValueError('Deleted task record is still present')
    for table in ('wrf_task_deletion_plans', 'wrf_forcing_cleanup_plans'):
        if db.execute('SELECT 1 FROM '+table+' WHERE status IN (?,?,?) LIMIT 1',
                      ('checking', 'executing', 'unknown')).fetchone():
            raise ValueError('Deletion is active or unknown')
    request = manifest['terminal_request']
    return {'task_id': row[0], 'deletion_id': deletion_id, 'deletion_digest': digest,
            'control': request['control'], 'attempt': request['attempt'], 'terminal': manifest['terminal']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deletion-id', required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    with (root / '.run/wrf-maintenance.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(settings.database_path.as_uri() + '?mode=ro', uri=True) as db:
            bound = approval(db, args.deletion_id, args.digest)
            program = 'import fcntl,hashlib,json,os,re,stat,time,sys\nfrom pathlib import Path\n'
            program += '\n'.join(inspect.getsource(fn) for fn in
                                  (compute_lease.mount_for_fd, compute_lease.private_fuseblk_group,
                                   compute_lease.read_lease, compute_lease.no_live_processes, compute_lease.reclaim))
            program += '\nprint(json.dumps(reclaim(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3]=="apply")))'
            command = 'python3 -B -c ' + shlex.quote(program) + ' ' + shlex.quote(settings.hpc_workbench_data_root)
            command += ' ' + shlex.quote(json.dumps(bound)) + ' ' + ('apply' if args.apply else 'check')
            receipt = json.loads(HpcClient(settings).run(command, timeout=30))
            print(json.dumps(receipt))


if __name__ == '__main__':
    main()
