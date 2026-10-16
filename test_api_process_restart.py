"""Real isolated API process restart; no production ports, stores or SSH."""
import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
import urllib.request

RUNNING = 'wrf_gfs_20261008T000000Z_aaaaaaaa'
QUEUED = 'wrf_ecmwf_20261008T000000Z_bbbbbbbb'
TOKEN = 'a' * 32


def serve(directory, descriptor):
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from unittest.mock import Mock
    from fastapi import FastAPI
    import uvicorn
    from config import settings
    from database import TaskStore
    from task_manager import WrfTaskManager

    root = Path(directory)
    isolated = replace(settings, database_path=root/'tasks.sqlite3',
        run_dir=root/'runs', data_dir=root/'data', runtime_asset_root=None,
        legacy_asset_manifest=None, ordinary_max_concurrent=1,
        hpc_gfs_prefetch_enabled=False)
    store = TaskStore(isolated.database_path)
    remote = Mock()
    remote.run.side_effect = AssertionError('SSH is forbidden in this fixture')
    manager = WrfTaskManager(isolated, store, None, remote)
    observed = []
    manager._check_health = lambda: None
    manager._gfs_prefetch_loop = lambda: manager._stop.wait()
    def observe(task_id):
        observed.append(task_id)
        manager._stop.wait()
    manager._execute = observe

    @asynccontextmanager
    async def lifespan(app):
        manager.start()
        try:
            yield
        finally:
            manager.stop()
            store.close()
            (root/('stopped-'+str(os.getpid()))).write_text('lifespan-complete')

    app = FastAPI(lifespan=lifespan)
    @app.get('/fixture')
    def state():
        return {'observed':observed, 'running':store.get(RUNNING),
                'queued':store.get(QUEUED), 'ssh_calls':remote.run.call_count}
    listener = socket.socket(fileno=descriptor)
    uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[listener])


class ApiProcessRestartTest(unittest.TestCase):
    def test_restart_preserves_attempt_and_blocks_queue_behind_unknown_session(self):
        from database import TaskStore
        from test_namelist_config import request
        with TemporaryDirectory(prefix='wrf-api-process-restart-') as directory:
            path = Path(directory)/'tasks.sqlite3'
            store = TaskStore(path)
            store.create(RUNNING, request(), 'fixture-owner')
            store.create(QUEUED, request(), 'fixture-owner')
            store.update(RUNNING, status='running', stage='running', runtime={
                'remote_launch_attempted':True, 'launch_attempt':TOKEN,
                'remote_pid':987654, 'remote_process':{
                    'pid':987654, 'start_ticks':'synthetic', 'boot_id':'synthetic'}})
            store.close()
            for generation in range(2):
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1',0))
                    listener.listen()
                    process = subprocess.Popen([sys.executable,'-B',__file__,
                        '--fixture',directory,str(listener.fileno())],
                        pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE)
                    try:
                        deadline = time.monotonic()+15
                        while True:
                            if process.poll() is not None:
                                self.fail(process.stderr.read().decode())
                            try:
                                with urllib.request.urlopen(
                                    'http://127.0.0.1:%d/fixture'%listener.getsockname()[1],
                                    timeout=.5) as response:
                                    state=json.load(response)
                                if state['observed']:break
                            except (OSError,TimeoutError):
                                pass
                            if time.monotonic()>deadline:self.fail('Isolated API did not become ready')
                            time.sleep(.05)
                        with self.subTest(generation=generation):
                            self.assertEqual(state['observed'],[RUNNING])
                            self.assertEqual(state['running']['status'],'reconciling')
                            self.assertEqual(state['running']['runtime']['launch_attempt'],TOKEN)
                            self.assertEqual(state['queued']['status'],'queued')
                            self.assertEqual(state['ssh_calls'],0)
                    finally:
                        process.terminate()
                        process.wait(timeout=15)
                        process.stderr.close()
                    self.assertIn(process.returncode,(0,-signal.SIGTERM))
                    self.assertEqual((Path(directory)/('stopped-'+str(process.pid))).read_text(),
                                     'lifespan-complete')


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--fixture':
        serve(sys.argv[2],int(sys.argv[3]))
    else:
        unittest.main()
