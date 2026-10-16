#!/usr/bin/env python3
"""Isolated short WRF MPI comparison with identity-checked pause/resume.

Operator-only diagnostic; does not publish products or change the managed job.
Compatible with the remote Python 3.6. Retains logs and its exact input identity.
"""
import argparse
import hashlib
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path


def identity(pid):
    fields = Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'start_ticks': fields[19], 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def same(value):
    try:
        return identity(value['pid']) == value
    except (OSError, ValueError, KeyError):
        return False


def resume(record):
    for item in record['ranks']:
        if same(item):
            os.kill(item['pid'], signal.SIGCONT)


def watch(root):
    record = json.loads((root / 'pause.json').read_text())
    while same(record['supervisor']) and time.time() < record['deadline']:
        if (root / 'resumed.json').is_file():
            return
        time.sleep(2)
    if same(record['supervisor']):
        os.kill(record['supervisor']['pid'], signal.SIGTERM)
        time.sleep(3)
    resume(record)
    (root / 'watchdog-resume.json').write_text(json.dumps({'time': time.time()}))


def short_namelist(text):
    match = re.search(r'&time_control\b(.*?)/', text, re.S | re.I)
    if not match:
        raise ValueError('Missing time_control')
    group = match.group(1)
    def number(key):
        found = re.search(r'\b' + key + r'\s*=\s*(\d+)', group, re.I)
        if not found:
            raise ValueError('Missing ' + key)
        return int(found.group(1))
    values = {'run_days': 0, 'run_hours': 0, 'run_minutes': 0, 'run_seconds': 8,
              'end_year': number('start_year'), 'end_month': number('start_month'),
              'end_day': number('start_day'), 'end_hour': number('start_hour'),
              'end_minute': 0, 'end_second': 8,
              'history_interval_s': 8, 'history_begin_s': 8, 'restart': '.false.'}
    for key, value in values.items():
        expression = r'\b' + key + r'\s*=\s*[^,\n]+,?'
        group, count = re.subn(expression, '{} = {},'.format(key, value), group, flags=re.I)
        if not count:
            group += '\n {} = {},\n'.format(key, value)
    return text[:match.start(1)] + group + text[match.end(1):]


def run(root, source, control, environment):
    active = json.loads((control / 'active-launch.json').read_text())['attempt']
    state = json.loads((control / 'launches' / active / 'state.json').read_text())
    if state.get('status') != 'running' or not same(state['process']):
        raise RuntimeError('Managed process is not verifiably running')
    session = os.getsid(state['process']['pid'])
    ranks = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / 'comm').read_text().strip() == 'wrf.exe' and (entry / 'cwd').resolve() == source and os.getsid(int(entry.name)) == session:
                ranks.append(identity(int(entry.name)))
        except (OSError, ValueError):
            continue
    if len(ranks) != 4:
        raise RuntimeError('Expected exactly four current ranks; found {}'.format(len(ranks)))
    root.mkdir(parents=True, exist_ok=False)
    record = {'supervisor': identity(os.getpid()), 'ranks': ranks, 'deadline': time.time() + 1200,
              'managed_attempt': active, 'source': str(source)}
    (root / 'pause.json').write_text(json.dumps(record, indent=2))
    with (root / 'watchdog.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--watch', str(root)],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    def interrupted(_signum, _frame):
        raise RuntimeError('Benchmark interrupted; restoring production ranks')
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    results = []
    child = None
    try:
        for rank in ranks:
            if not same(rank):
                raise RuntimeError('Rank identity changed before pause')
            os.kill(rank['pid'], signal.SIGSTOP)
        namelist = short_namelist((source / 'namelist.input').read_text())
        for index, count in enumerate((4, 8, 10, 4)):
            case = root / '{}-mpi{}'.format(index + 1, count)
            case.mkdir()
            for original in source.iterdir():
                if original.is_symlink() or original.name in {'wrfinput_d01', 'wrfbdy_d01', 'wrf_les_iofields.txt'}:
                    (case / original.name).symlink_to(original.resolve())
            (case / 'namelist.input').write_text(namelist)
            command = '. "$1" >/dev/null 2>&1 && exec mpirun -np "$2" ./wrf.exe'
            started = time.time()
            with (case / 'benchmark.log').open('wb') as log:
                child = subprocess.Popen(['bash', '-c', command, 'benchmark', str(environment), str(count)],
                                         cwd=str(case), start_new_session=True, stdout=log, stderr=log)
                code = child.wait(timeout=240)
            child = None
            logs = '\n'.join(path.read_text(errors='replace') for path in case.glob('rsl.error.*'))
            if code or re.search(r'points\s+exceeded\s+[vh]_cfl|\bNaN\b|\bFATAL\b|segmentation', logs, re.I) or 'SUCCESS COMPLETE WRF' not in logs:
                raise RuntimeError('Benchmark failed: {}'.format(case))
            values = [float(value) for value in re.findall(r'Timing for main:.*?on domain\s+1:\s+([\d.]+) elapsed', (case / 'rsl.error.0000').read_text())]
            if len(values) < 100:
                raise RuntimeError('Insufficient samples: {}'.format(len(values)))
            results.append({'mpi': count, 'case': str(case), 'steps': len(values),
                            'median_step_seconds': statistics.median(values[16:]),
                            'wall_seconds': time.time() - started,
                            'namelist_sha256': hashlib.sha256(namelist.encode()).hexdigest()})
            (root / 'results.json').write_text(json.dumps(results, indent=2))
    finally:
        try:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        finally:
            resume(record)
            (root / 'resumed.json').write_text(json.dumps({'time': time.time(), 'identities_alive': [same(item) for item in ranks]}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', type=Path)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--control', type=Path)
    parser.add_argument('--environment', type=Path)
    args = parser.parse_args()
    if args.watch:
        watch(args.watch)
    else:
        run(args.root.resolve(), args.source.resolve(), args.control.resolve(), args.environment.resolve())


if __name__ == '__main__':
    main()
