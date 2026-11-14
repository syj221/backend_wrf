"""Product isolation and split-file transaction regressions for IFS 0.1 degrees."""
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from config import Settings
from database import TaskStore
from gfs import GfsManager
from hpc import _write_expected_forcing_index
from hpc_transport import HpcClient, SAFE_TASK_ID
from scripts import forcing_pool_queue as pool
from test_forcing_pool_queue import args

CYCLE = '2026102800'


@pytest.fixture(autouse=True)
def isolated_kernel_locks(tmp_path, monkeypatch):
    """Keep synthetic queue locks out of the shared runtime lock directory."""
    monkeypatch.setattr(pool, 'LOCK_ROOT', tmp_path / 'kernel-locks')


def queue(root):
    return pool.SplitEcmwfQueue(args(root, 'ecmwf_0p1'))


def succeed(command, **kwargs):
    Path(command[command.index('--output') + 1]).write_bytes(b'GRIBreal-test-payload7777')
    return subprocess.CompletedProcess(command, 0, '1234', '')


def test_split_step_commits_both_components_and_retains_component_history(tmp_path):
    q = queue(tmp_path / 'ecmwf_0p1')
    q.enqueue(CYCLE, [0], 'task', 'forcing_' + 'a'*32)
    with patch.object(pool._EcmwfComponentQueue, 'run_curl', side_effect=succeed):
        q._download_one_locked(CYCLE, 0)
    entry = q.load_state()['cycles'][CYCLE]['files']['000']
    assert entry['status'] == 'ready'
    assert set(entry['components']) == {'sfc', 'pl'}
    assert q.file_path(CYCLE, 0).read_bytes() == b'GRIBreal-test-payload7777' * 2
    assert entry['references'] == {'forcing_' + 'a'*32: True}
    assert {row['component'] for row in q.attempt_history(CYCLE)['items']} == {'sfc', 'pl'}
    with patch.object(pool._EcmwfComponentQueue, 'run_curl', side_effect=AssertionError('must reuse')):
        q._download_one_locked(CYCLE, 0)


def test_interrupted_pl_preserves_sfc_partial_and_references_then_resumes(tmp_path):
    q = queue(tmp_path / 'high')
    q.enqueue(CYCLE, [0], 'task', 'forcing_' + 'd'*32)
    commands = []
    def transfer(command, **kwargs):
        commands.append(command)
        if command[-1].endswith('sfc.grib2'):
            return succeed(command, **kwargs)
        Path(command[command.index('--output') + 1]).write_bytes(b'GRIBpartial')
        return subprocess.CompletedProcess(command, 28, '0', 'Operation timed out')
    with patch.object(pool._EcmwfComponentQueue, 'run_curl', side_effect=transfer):
        q._download_one_locked(CYCLE, 0)
    assert not q.file_path(CYCLE, 0).exists()
    assert (q.root / CYCLE / q.component_name(0, 'sfc')).exists()
    assert q.part_size(CYCLE, 0) == len(b'GRIBpartial')
    assert q.load_state()['cycles'][CYCLE]['files']['000']['references'] == {'forcing_' + 'd'*32: True}
    child = pool._EcmwfComponentQueue(q, CYCLE, 0, 'pl')
    child._set_entry(CYCLE, 0, {'retry_at': 0, 'terminal_retry_at': 0})
    resumed = []
    def finish(command, **kwargs):
        resumed.append(command[-1])
        assert '--continue-at' in command
        return succeed(command, **kwargs)
    with patch.object(pool._EcmwfComponentQueue, 'run_curl', side_effect=finish):
        q._download_one_locked(CYCLE, 0)
    assert len(resumed) == 1 and resumed[0].endswith('pl.grib2')
    history = q.attempt_history(CYCLE)['items']
    assert len(history) == 3
    assert {r['status'] for r in history} == {'succeeded', 'failed'}


def test_both_publication_components_required(tmp_path):
    q = queue(tmp_path)
    q.enqueue(CYCLE, [0], 'prefetch')
    def probe(command, **kwargs):
        if command[-1].endswith('sfc.grib2'):
            return succeed(command, **kwargs)
        return subprocess.CompletedProcess(command, 22, '', 'HTTP 404')
    with patch.object(q, 'run_curl', side_effect=probe):
        assert q.probe_published(CYCLE) is False
    assert q.load_state()['cycles'][CYCLE]['publication'] == 'waiting_publish'
    assert not q.file_path(CYCLE, 0).exists()


def test_same_cycle_reference_and_cleanup_are_resolution_scoped(tmp_path):
    old = pool.PoolQueue(args(tmp_path / 'ecmwf'))
    high = queue(tmp_path / 'ecmwf_0p1')
    old.enqueue(CYCLE, [0], 'task', 'forcing_' + 'b'*32)
    high.enqueue(CYCLE, [0], 'task', 'forcing_' + 'c'*32)
    old.file_path(CYCLE, 0).parent.mkdir(parents=True)
    old.file_path(CYCLE, 0).write_bytes(b'GRIBold-untouched7777')
    old.release_reference(CYCLE, 'forcing_' + 'c'*32)
    assert old.load_state()['cycles'][CYCLE]['files']['000']['references'] == {'forcing_' + 'b'*32: True}
    high.file_path(CYCLE, 0).parent.mkdir(parents=True)
    (high.root / CYCLE / (high.component_name(0, 'pl') + '.part')).write_bytes(b'GRIBowned-part')
    high.release_reference(CYCLE, 'forcing_' + 'c'*32)
    assert high.cleanup(CYCLE) == 'DELETED'
    assert not high.file_path(CYCLE, 0).parent.exists()
    assert old.file_path(CYCLE, 0).parent.exists()
    assert old.cleanup(CYCLE) == 'BLOCKED'
    assert old.expected_hours() == high.expected_hours() == tuple(range(0, 73, 3))


def test_resolution_specific_paths_names_task_identity_and_submit_index(tmp_path):
    h = HpcClient(Settings())
    old, high = h._forcing_settings('ecmwf'), h._forcing_settings('ecmwf_0p1')
    assert old['root'] != high['root']
    assert old['product'] != high['product']
    assert high['total_files'] == 25
    assert SAFE_TASK_ID.fullmatch('wrf_ecmwf_0p1_20261028T000000Z_1234abcd')
    item = {'name': high['file_name'](0), 'forecast_hour': 0, 'size': 10, 'sha256': 'a'*64}
    assert h._manifest_entry_forcing('ecmwf', item, CYCLE) is None
    assert h._manifest_entry_forcing('ecmwf_0p1', item, CYCLE)
    _write_expected_forcing_index('ecmwf_0p1', CYCLE, [0], [item], tmp_path / 'expected.tsv')
    assert '0p1' in (tmp_path / 'expected.tsv').read_text()
    assert GfsManager(Settings()).latest_cycles(data_source='ecmwf') == GfsManager(Settings()).latest_cycles(data_source='ecmwf_0p1')


def test_old_policy_read_adds_empty_product_hold_without_database_migration(tmp_path):
    store = TaskStore(tmp_path / 'tasks.db')
    try:
        store.save_forcing_policy(0, True, True, {'gfs': [], 'ecmwf': [CYCLE]})
        before = tuple(store._db.execute('SELECT * FROM wrf_forcing_policy').fetchone())
        assert store.forcing_policy()['held_cycles']['ecmwf_0p1'] == []
        assert tuple(store._db.execute('SELECT * FROM wrf_forcing_policy').fetchone()) == before
    finally:
        store.close()


def test_lone_surface_and_changed_pair_are_never_ready(tmp_path):
    q = queue(tmp_path)
    q.enqueue(CYCLE, [0], 'prefetch')
    output = q.file_path(CYCLE, 0)
    output.parent.mkdir(parents=True)
    output.write_bytes(b'GRIBsurface-only7777')
    assert not q.valid(output)
    with patch.object(pool._EcmwfComponentQueue, 'run_curl', side_effect=succeed):
        q._download_one_locked(CYCLE, 0)
    assert q.valid(output)
    output.write_bytes(b'GRIBchanged-pair7777')
    assert not q.valid(output)


def test_split_rejects_external_component_symlinks_and_overlap(tmp_path):
    q = queue(tmp_path / 'product')
    q.enqueue(CYCLE, [0], 'prefetch')
    (q.root / CYCLE).mkdir()
    external = tmp_path / 'preserve'
    external.write_bytes(b'preserve me')
    (q.root / CYCLE / q.component_name(0, 'sfc')).symlink_to(external)
    with pytest.raises(ValueError, match='Unsafe'):
        q._download_one_locked(CYCLE, 0)
    assert external.read_bytes() == b'preserve me'
    from dataclasses import replace
    with pytest.raises(ValueError, match='独立资料根目录'):
        HpcClient(replace(Settings(), hpc_ec_0p1_dir=Settings().hpc_ec_dir))


def test_semantic_check_rejects_renamed_quarter_degree_fields(tmp_path):
    import sys
    import numpy as np
    from types import SimpleNamespace
    from scripts.validate_forcing import validate
    path = tmp_path / 'renamed.grib2'
    path.write_bytes(b'fixture')
    metadata = dict(shortName='t', typeOfLevel='isobaricInhPa', level=1000, offset=0, totalLength=7,
                    validityDate=20261028, validityTime=0, md5GridSection='grid',
                    gridType='regular_ll', iDirectionIncrementInDegrees=0.25,
                    jDirectionIncrementInDegrees=0.25)
    def inspect(expected):
        handles = iter([1, None])
        fake = SimpleNamespace(codes_get=lambda h,k:metadata[k],
            codes_get_array=lambda h,k:np.array([-90.,90.]),
            codes_grib_new_from_file=lambda f:next(handles), codes_release=lambda h:None)
        with patch.dict(sys.modules, {'eccodes':fake}):
            return validate(path, expected_spacing=expected)
    assert 'grid_spacing_mismatch' in inspect(0.1)['issues']
    assert 'grid_spacing_mismatch' not in inspect(None)['issues']


def test_foreign_queue_or_history_is_not_read_through_a_resolution_alias():
    from hpc_transport import HpcError
    h = HpcClient(Settings())
    h.run = lambda *a, **kw: json.dumps({'version':1,'source':'ecmwf','cycles':{CYCLE:{'files':{}}}})
    with pytest.raises(HpcError, match='归属不一致'):
        h._pool_queue_state(h.settings.hpc_ec_0p1_dir)
    with pytest.raises(HpcError, match='归属不一致'):
        h.forcing_attempt_history('ecmwf_0p1', CYCLE)
    assert h._pool_queue_state(h.settings.hpc_ec_dir)['source'] == 'ecmwf'


def test_task_window_ready_does_not_promote_an_incomplete_pool_cycle():
    from unittest.mock import Mock
    for source in ('ecmwf','ecmwf_0p1'):
        h = HpcClient(Settings())
        h._pool_queue_state = Mock(return_value={'source':source,'cycles':{CYCLE:{'files':{'000':{'status':'ready'},'003':{'status':'ready'}}}}})
        h.run = Mock(return_value='ROOT|/pool\nHOURS|2026102800|000,003\n2026102800|partial|2|100|3|0|0|0|')
        cycle = h.forcing_pool_items(source,[CYCLE],set())[0]['cycles'][0]
        assert cycle['completed_files'] == 2 and cycle['total_files'] == 25
        assert not cycle['complete'] and cycle['status'] != 'ready'
