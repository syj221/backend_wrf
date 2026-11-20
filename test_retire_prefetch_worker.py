"""Native stopped-process tests, reusable on a synthetic actual-volume folder."""
import json
import os
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import patch

import test_pause_pool_worker as fixture
h, q = fixture.h, fixture.q
import retire_prefetch_worker as retire


@unittest.skipUnless(sys.platform == "linux", "native pidfd required")
class ObsoletePrefetchStop(unittest.TestCase):
    wait = fixture.PauseTest.wait
    stop = fixture.PauseTest.stop
    # Do not inherit the old bridge's test methods; use only its TLS/process fixture.
    def setUp(self):
        fixture.PauseTest.setUp(self)
        self.worker.send_signal(signal.SIGSTOP)
        for pid in h.children(self.worker.pid): os.kill(pid, signal.SIGSTOP)
        self.wait(lambda: retire.stopped(self.worker.pid) and all(retire.stopped(pid) for pid in h.children(self.worker.pid)))
        self.pool.release_reference('2026102000', 'forcing_' + 'b' * 32)
        self.pool.expire_prefetch([])
        self.binding = patch.object(q, '__file__', str(self.old)); self.binding.start(); self.addCleanup(self.binding.stop)
        self.retire_plan = retire.prepare(q, str(self.root), '2026102000')

    def cleanup_worker(self):
        # The original fixture assumes a running curl; allow a failed-test cleanup
        # to reap a stopped fixture only. No production process is used in tests.
        if self.worker.poll() is None:
            for pid in h.children(self.worker.pid):
                try: os.kill(pid, signal.SIGTERM); os.kill(pid, signal.SIGCONT)
                except ProcessLookupError: pass
        fixture.PauseTest.cleanup_worker(self)

    def test_stop_preserves_every_file_and_history_without_spawn(self):
        files = retire.inventory(self.root / '2026102000')
        with patch('subprocess.Popen') as spawn:
            result = retire.retire(self.retire_plan, q, self.audit / 'retire.json', self.retire_plan['digest'])
        spawn.assert_not_called()
        assert result['status'] == 'complete'
        assert retire.inventory(self.root / '2026102000') == files
        assert self.part.stat().st_ino == self.inode
        assert self.pool.load_state()['cycles']['2026102000']['files']['012']['status'] == 'cancelled'
        assert self.pool._pick_next() is None
        assert self.pool.attempt_history('2026102000')['items'][0]['cause'] == 'obsolete_prefetch_stopped'
        with self.assertRaisesRegex(RuntimeError, 'never replay'):
            retire.retire(self.retire_plan, q, self.audit / 'retire.json', self.retire_plan['digest'])

    def test_reference_added_after_plan_blocks_all_signals(self):
        self.pool.enqueue('2026102000', [12], 'task', 'forcing_' + 'c' * 32)
        with patch.object(h, 'pidfd_signal') as send:
            with self.assertRaisesRegex(RuntimeError, 'references'):
                retire.retire(self.retire_plan, q, self.audit / 'reference.json', self.retire_plan['digest'])
        send.assert_not_called()
        assert retire.stopped(self.worker.pid)

    def test_changed_process_identity_blocks_all_signals(self):
        self.retire_plan['workers'][0]['process']['start_ticks'] = 'invalid'
        self.retire_plan['digest'] = h.digest({k: v for k, v in self.retire_plan.items() if k != 'digest'})
        with patch.object(h, 'pidfd_signal') as send:
            with self.assertRaisesRegex(RuntimeError, 'bindings changed'):
                retire.retire(self.retire_plan, q, self.audit / 'identity.json', self.retire_plan['digest'])
        send.assert_not_called()

    def test_unknown_exit_never_clears_queue_or_replays(self):
        with patch.object(retire, 'exited', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'exit unknown'):
                retire.retire(self.retire_plan, q, self.audit / 'unknown.json', self.retire_plan['digest'])
        assert self.pool.load_state()['cycles']['2026102000']['files']['012']['status'] == 'downloading'
        assert json.loads((self.audit / 'unknown.json').read_text())['status'] == 'needs_review'
