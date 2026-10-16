"""Regression coverage for historical launch evidence on a cancelled newer attempt."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from terminal_evidence import WAITING_PROBE, prior_attempt_bindings


class HistoricalTerminalTest(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='wrf-history-proof-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control = self.root / 'task-identity' / 'control'
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.new, self.old = 'a' * 32, 'b' * 32
        self.identity = {'pid': 5278, 'start_ticks': '88491644',
                         'boot_id': '56365d4c-453f-478a-9a77-49332b34707d'}
        for token in (self.new, self.old):
            (self.control / 'launches' / token).mkdir(parents=True)
        self.write(self.new, {'attempt': self.new, 'status': 'failed', 'cancelled': True,
            'cancellation_verified': True, 'reason': 'cancelled_before_launch'})
        self.write(self.old, {'attempt': self.old, 'status': 'failed',
            'process': self.identity, 'exit_code': 141})
        for name, value in {'launch.lock': '', 'service.pid': '5278', 'exit.code': '141',
            'process.json': json.dumps(self.identity),
            'active-launch.json': json.dumps({'attempt': self.old})}.items():
            (self.control / name).write_text(value)
        self.binding = {'launch_attempt': self.old, 'remote_process': self.identity,
            'remote_pid': 5278, 'remote_terminal_status': 'failed',
            'remote_task_dir': str(self.control)}
        self.runtime = {'launch_attempt': self.new, 'remote_process': None,
                         '_terminal_prior_attempts': [self.binding]}

    def write(self, token, value):
        (self.control / 'launches' / token / 'state.json').write_text(json.dumps(value))

    def probe(self, success=False):
        program = WAITING_PROBE.replace("Path('/proc')", 'Path(' + repr(str(self.proc)) + ')')
        result = subprocess.run([sys.executable, '-B', '-c', program, 'managed',
            str(self.control), json.dumps(self.runtime)], text=True, capture_output=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_bound_failed_history_is_terminal_without_rewriting_evidence(self):
        before = {str(p): p.read_bytes() for p in self.control.rglob('*') if p.is_file()}
        proof = self.probe(True)
        self.assertEqual(proof['historical_terminal_attempts'][0]['exit_code'], 141)
        self.assertTrue(proof['never_started'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.control.rglob('*') if p.is_file()})

    def test_missing_history_blocks(self):
        self.runtime['_terminal_prior_attempts'] = []
        self.probe()

    def test_missing_remote_history_blocks(self):
        import shutil
        shutil.rmtree(self.control / 'launches' / self.old)
        self.probe()

    def test_duplicate_local_binding_blocks(self):
        self.runtime['_terminal_prior_attempts'].append(dict(self.binding))
        self.probe()

    def test_identity_mismatch_blocks(self):
        self.binding['remote_process'] = dict(self.identity, start_ticks='123')
        self.probe()

    def test_exit_mismatch_blocks(self):
        (self.control / 'exit.code').write_text('0')
        self.probe()

    def test_unknown_active_attempt_blocks(self):
        (self.control / 'active-launch.json').write_text(json.dumps({'attempt': 'c' * 32}))
        self.probe()

    def test_unknown_sibling_blocks(self):
        (self.control / 'launches' / ('c' * 32)).mkdir()
        self.probe()

    def fake_process(self, pid, sid, command=b'worker'):
        folder = self.proc / str(pid)
        folder.mkdir()
        fields = ['S', '1', '1', str(sid)] + ['0'] * 16
        (folder / 'stat').write_text(str(pid) + ' (worker) ' + ' '.join(fields))
        (folder / 'cmdline').write_bytes(command)

    def test_old_session_live_blocks(self):
        self.fake_process(9999, 5278)
        self.probe()

    def test_reused_pid_blocks(self):
        self.fake_process(5278, 9998)
        self.probe()

    def test_orphan_task_process_blocks(self):
        self.fake_process(9999, 9998, str(self.control.parent).encode())
        self.probe()

    def test_pending_cancellation_blocks(self):
        (self.control / 'launches' / self.old / 'cancellation.json').write_text('{"status":"pending"}')
        self.probe()

    def test_symlink_marker_blocks(self):
        marker = self.control / 'service.pid'
        marker.rename(self.root / 'pid')
        marker.symlink_to(self.root / 'pid')
        self.probe()

    def test_busy_launch_lock_blocks(self):
        import fcntl
        with (self.control / 'launch.lock').open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.probe()

    def test_local_history_only_older_same_task_and_current_binding(self):
        database = self.root / 'history.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE wrf_tasks(id TEXT,attempt_no INTEGER,runtime_json TEXT)')
            db.execute('CREATE TABLE wrf_task_attempts(task_id TEXT,attempt_no INTEGER,runtime_json TEXT)')
            db.execute('INSERT INTO wrf_tasks VALUES(?,?,?)', ('target', 2, json.dumps(self.runtime)))
            for task, number in [('target', 1), ('other', 1), ('target', 2)]:
                db.execute('INSERT INTO wrf_task_attempts VALUES(?,?,?)', (task, number, json.dumps(self.binding)))
        task = {'id': 'target', 'attempt_no': 2, 'runtime': self.runtime}
        self.assertEqual(len(prior_attempt_bindings(SimpleNamespace(database_path=database), task)), 1)
        task['runtime'] = {'launch_attempt': 'c' * 32}
        with self.assertRaisesRegex(RuntimeError, 'binding changed'):
            prior_attempt_bindings(SimpleNamespace(database_path=database), task)
