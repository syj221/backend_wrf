"""Live process observations must not weaken download or cleanup protection."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from config import Settings
from hpc_transport import HpcClient
from pool_observation import observe_worker


def fixture(tmp_path, worker_state='T', child_state='T'):
    root = tmp_path / 'gfs'; root.mkdir()
    marker = root / '.pool-worker.lockdir'; marker.mkdir()
    (marker / 'pid').write_text('100')
    (marker / 'owner.json').write_text(json.dumps({'protocol': 'wrf-pool-worker-2', 'pid': 100, 'root': str(marker)}))
    proc = tmp_path / 'proc'; proc.mkdir()
    def process(pid, state, ppid, args, children):
        path = proc / str(pid); path.mkdir()
        fields = [state, str(ppid)] + ['0'] * 17 + ['12345']
        (path / 'stat').write_text(str(pid) + ' (fixture) ' + ' '.join(fields))
        (path / 'cmdline').write_bytes(('\0'.join(args) + '\0').encode())
        task = path / 'task' / str(pid); task.mkdir(parents=True)
        (task / 'children').write_text(children)
    process(100, worker_state, 1, ['python3', '/release/forcing_pool_queue.py', 'worker', '--root', str(root), '--source', 'gfs'], '101')
    process(101, child_state, 100, ['curl', '--output', str(root / '2026111000' / 'gfs.f051.part')], '')
    return root, proc


@pytest.mark.parametrize('parent,child,expected', [('T','T','paused'), ('S','T','paused'), ('T','S','running'), ('S','S','running')])
def test_only_confirmed_stopped_transfer_is_paused(tmp_path, parent, child, expected):
    root, proc = fixture(tmp_path, parent, child)
    assert observe_worker(root, 'gfs', proc)['state'] == expected


@pytest.mark.parametrize('damage', ['source', 'pid', 'child', 'missing', 'owner'])
def test_unknown_identity_never_infers_running_or_pause(tmp_path, damage):
    root, proc = fixture(tmp_path)
    if damage == 'source': source = 'ecmwf'
    else: source = 'gfs'
    if damage == 'pid': (root / '.pool-worker.lockdir/pid').write_text('102')
    if damage == 'child': (proc / '101/cmdline').write_bytes(b'curl\0--output\0/outside/file.part\0')
    if damage == 'missing': (proc / '100/stat').unlink()
    if damage == 'owner': (root / '.pool-worker.lockdir/owner.json').write_text('{}')
    assert observe_worker(root, source, proc) == {'state': 'unknown'}


@pytest.mark.parametrize('source,total', [('gfs',73), ('ecmwf',25), ('ecmwf_0p1',25)])
@pytest.mark.parametrize('observed,expected', [('paused','paused'), ('unknown','status_unknown'), ('running','downloading')])
def test_pool_state_and_cleanup_remain_protected(source, total, observed, expected):
    client = HpcClient(Settings())
    state = {'source': source, '_worker': {'state': observed, 'transfers': [
        {'cycle': '2026111000', 'file': 'f051', 'state': 'T' if observed == 'paused' else 'S'}]},
        'cycles': {'2026111000': {'publication': 'published', 'files': {'051': {
            'status': 'downloading', 'current_file': 'f051', 'download_rate_bps': 999}}},
                   '2026111800': {'publication': 'published', 'files': {'000': {'status': 'queued'}}}}}
    # Unknown observation deliberately contains no trusted transfer evidence.
    if observed == 'unknown': state['_worker']['transfers'] = []
    with patch.object(client, '_pool_queue_state', return_value=state), patch.object(client, 'run', return_value='ROOT|/pool\n2026111000|partial|1|100|0|1|100|0|'):
        pool = client.forcing_pool_items(source, ['2026111800'], set())[0]
    cycle = pool['cycles'][0]
    assert cycle['status'] == pool['status'] == expected
    assert not cycle['cleanup_allowed'] and not cycle['auto_cleanup_allowed']
    assert cycle['download_rate_bps'] == (999 if observed == 'running' else 0)
    queued = client._queue_cycle_details(state, '2026111800')
    assert queued['queue_status'] == 'queued'
    if observed == 'paused': assert '工作器已暂停' in queued['wait_reason']


def test_changed_start_ticks_fails_closed(tmp_path):
    root, proc = fixture(tmp_path)
    from pool_observation import process
    count = 0
    def changing(pid, proc_root):
        nonlocal count
        value = process(pid, proc_root)
        if pid == 100:
            count += 1
            if count > 1: value['start_ticks'] = '67890'
        return value
    with patch('pool_observation.process', side_effect=changing):
        assert observe_worker(root, 'gfs', proc)['state'] == 'unknown'
