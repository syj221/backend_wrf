"""Read-only Linux pool telemetry. Portable to the remote Python 3.6 runtime."""
import json
import os
from pathlib import Path


def process(pid, proc_root):
    path = Path(proc_root) / str(pid)
    before = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    if path.stat().st_uid != os.getuid():
        raise ValueError('foreign process')
    argv = (path / 'cmdline').read_bytes().decode().strip('\0').split('\0')
    after = (path / 'stat').read_text().rsplit(')', 1)[1].split()
    if before[19] != after[19]:
        raise ValueError('process identity changed')
    return {'pid': pid, 'start_ticks': after[19], 'state': after[0],
            'ppid': int(after[1]), 'argv': argv}


def observe_worker(root, source, proc_root='/proc'):
    """Never infer a pause merely from old timestamps or lack of byte growth."""
    marker = root / '.pool-worker.lockdir'
    try:
        if marker.is_symlink() or marker.stat().st_uid != os.getuid():
            raise ValueError('foreign marker')
        owner = json.loads((marker / 'owner.json').read_text())
        pid = int((marker / 'pid').read_text())
        if (owner.get('protocol') != 'wrf-pool-worker-2' or owner.get('pid') != pid
                or owner.get('root') != str(marker.resolve())):
            raise ValueError('worker marker mismatch')
        worker = process(pid, proc_root)
        argv = worker['argv']
        script = next(i for i, arg in enumerate(argv[:3]) if Path(arg).name == 'forcing_pool_queue.py')
        if (argv[script + 1] != 'worker' or argv.count('--root') != 1 or argv.count('--source') != 1
                or Path(argv[argv.index('--root') + 1]).resolve() != root
                or argv[argv.index('--source') + 1] != source):
            raise ValueError('worker scope mismatch')
        children = {int(pid) for task in (Path(proc_root) / str(pid) / 'task').iterdir()
                    for pid in (task / 'children').read_text().split()}
        transfers = []
        for child_pid in children:
            child = process(child_pid, proc_root)
            args = child['argv']
            if child['ppid'] != pid or Path(args[0]).name != 'curl' or args.count('--output') != 1:
                raise ValueError('unknown child')
            part = Path(args[args.index('--output') + 1]).resolve()
            if (part.parent.parent != root or not part.parent.name.isdigit()
                    or len(part.parent.name) != 10 or not part.name.endswith('.part')):
                raise ValueError('foreign transfer')
            transfers.append({'cycle': part.parent.name, 'file': part.name[:-5], 'state': child['state']})
        latest = process(pid, proc_root)
        if latest['start_ticks'] != worker['start_ticks'] or latest['argv'] != worker['argv']:
            raise ValueError('worker changed during observation')
        paused = (transfers and all(row['state'] in {'T', 't'} for row in transfers)
                  or not transfers and latest['state'] in {'T', 't'})
        if latest['state'] in {'Z', 'X', 'x'}:
            return {'state': 'unknown'}
        return {'state': 'paused' if paused else 'running', 'transfers': transfers}
    except (OSError, ValueError, IndexError, KeyError, StopIteration, TypeError):
        # An unreadable or changed identity remains protected; never clear locks.
        return {'state': 'unknown'}


def read_pool(raw_root):
    root = Path(raw_root).expanduser().resolve()
    try:
        state = json.loads((root / '.pool-queue.json').read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict) or not isinstance(state.get('cycles'), dict):
        return {}
    state['_worker'] = observe_worker(root, state.get('source'))
    return state
