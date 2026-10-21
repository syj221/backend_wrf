"""Native process and actual data-volume pause tests; synthetic queues only."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parent/'scripts'))
import handoff_pool_workers as h
import pause_pool_worker as p
import forcing_pool_queue as q

OLD = r'''
import argparse,importlib.util,os,subprocess,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('q',os.environ['PAUSE_QUEUE']);q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)
a=argparse.ArgumentParser();a.add_argument('action');a.add_argument('--root');a.add_argument('--source');v=a.parse_args();pool=q.PoolQueue(v);cycle='2026102000'
with q.exclusive_process(pool.worker_lock) as worker:
 with q.kernel_guard(pool.root/'.pool-download-slot') as transfer:
  with q.exclusive(pool.cycle_lock(cycle)):
   pool._set_entry(cycle,12,{'status':'downloading','current_file':pool.file_name(12),'attempts':1})
   part=Path(str(pool.file_path(cycle,12))+'.part');part.parent.mkdir(exist_ok=True)
   cmd=['curl','--silent','--show-error','--insecure','--noproxy','*','--connect-to','data.ecmwf.int:443:127.0.0.1:'+os.environ['PAUSE_PORT'],'--continue-at','-','--output',str(part),'https://data.ecmwf.int/forecasts/20261020/00z/ifs/0p25/oper/20261020000000-12h-oper-fc.grib2']
   subprocess.run(cmd,pass_fds=(worker,transfer))
   time.sleep(60)
'''
NEW = r'''
import argparse,importlib.util,json,os,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('q',os.environ['PAUSE_QUEUE']);q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)
a=argparse.ArgumentParser();a.add_argument('action');a.add_argument('--root');a.add_argument('--source');v=a.parse_args();pool=q.PoolQueue(v)
with q.exclusive_process(pool.worker_lock) as worker:
 if worker is None:raise RuntimeError('duplicate worker')
 pool.recover_interrupted_downloads()
 with q.kernel_guard(pool.root/'.pool-download-slot'):
  with q.exclusive(pool.cycle_lock('2026102000')):
   part=Path(str(pool.file_path('2026102000',12))+'.part')
   with part.open('ab') as stream:stream.write(b'resumed7777')
   pool._set_entry('2026102000',12,{'status':'ready','bytes':part.stat().st_size})
   (pool.root/'resumed.json').write_text(json.dumps({'inode':part.stat().st_ino,'bytes':part.stat().st_size}))
'''

@unittest.skipUnless(sys.platform=='linux','native pidfd required')
class PauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='.pause-test-',dir=os.environ.get('HANDOFF_TEST_DATA_ROOT'));self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.root=self.base/'ecmwf';self.root.mkdir();(self.root/'logs').mkdir()
        self.old=self.base/'old.py';self.new=self.base/'new.py';self.old.write_text(OLD);self.new.write_text(NEW)
        self.pool=q.PoolQueue(argparse.Namespace(root=str(self.root),source='ecmwf'))
        self.pool.enqueue('2026102000',[12],'task','forcing_'+'b'*32)
        self.part=Path(str(self.pool.file_path('2026102000',12))+'.part');self.part.parent.mkdir();self.part.write_bytes(b'GRIBoriginal')
        self.inode=self.part.stat().st_ino
        self.audit_tmp=tempfile.TemporaryDirectory(prefix='pause-audit-');self.addCleanup(self.audit_tmp.cleanup);self.audit=Path(self.audit_tmp.name)
        cert=self.audit/'cert.pem';key=self.audit/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost','-days','1'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        server=r'''import http.server,ssl,sys,time
from pathlib import Path
class H(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(206);self.send_header('Content-Range','bytes 12-999999/1000000');self.send_header('Content-Length','999988');self.end_headers()
  try:
   while True:self.wfile.write(b'x'*1024);self.wfile.flush();time.sleep(.05)
  except (BrokenPipeError,ConnectionResetError):pass
 def log_message(self,*a):pass
s=http.server.HTTPServer(('127.0.0.1',0),H);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(sys.argv[1],sys.argv[2]);s.socket=ctx.wrap_socket(s.socket,server_side=True);Path(sys.argv[3]).write_text(str(s.server_port));s.serve_forever()
'''
        self.server=subprocess.Popen([sys.executable,'-c',server,str(cert),str(key),str(self.audit/'port')]);self.addCleanup(self.stop,self.server)
        self.wait(lambda:(self.audit/'port').exists())
        env={**os.environ,'PAUSE_QUEUE':os.environ.get('HANDOFF_QUEUE_MODULE',str(Path(q.__file__).resolve())),'PAUSE_PORT':(self.audit/'port').read_text()}
        self.environment=patch.dict(os.environ,env);self.environment.start();self.addCleanup(self.environment.stop)
        self.worker=subprocess.Popen([sys.executable,str(self.old),'worker','--source','ecmwf','--root',str(self.root)],env=env)
        self.addCleanup(self.cleanup_worker)
        self.wait(lambda:self.part.stat().st_size>12)
        original=h.prepare({'ecmwf':str(self.root)},str(self.old),str(self.new))
        original_path=self.audit/'original.json'
        h.save_audit(original_path,{'plan':original,'plan_digest':original['digest'],'status':'needs_review','error':'TimeoutError: test boundary','providers':{'gfs':{'status':'new_worker_started'}}})
        self.prior={'plan':original,'audit':str(original_path)};self.withdrawal=self.audit/'withdraw.json'
        p.withdraw(original_path,original,self.withdrawal)
        self.plan=p.prepare(str(self.old),str(self.new),str(self.root),self.prior,self.withdrawal)
    def wait(self,test):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            if test():return
            time.sleep(.03)
        self.fail('fixture not ready')
    def stop(self,proc):
        if proc.poll() is None:proc.terminate()
        proc.wait(timeout=5)
    def cleanup_worker(self):
        if self.worker.poll() is None:
            for pid in h.children(self.worker.pid):
                fd=h.pidfd_open(pid)
                try:h.pidfd_signal(fd,15)
                finally:os.close(fd)
            self.worker.send_signal(18);self.worker.terminate()
        self.worker.wait(timeout=5)
    def test_pause_preserves_partial_reference_and_resumes(self):
        result=p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
        self.assertEqual(result['status'],'new_worker_started');self.worker.wait(timeout=5)
        self.wait(lambda:(self.root/'resumed.json').exists())
        self.assertEqual(self.part.stat().st_ino,self.inode);self.assertTrue(self.part.read_bytes().startswith(b'GRIBoriginal'))
        self.assertTrue(self.part.read_bytes().endswith(b'resumed7777'))
        self.assertEqual(self.pool.load_state()['cycles']['2026102000']['files']['012']['references'],{'forcing_'+'b'*32:True})
        row=self.pool.attempt_history('2026102000')['items'][0]
        self.assertEqual((row['status'],row['cause']),('interrupted','operator_pause'))
        with self.assertRaisesRegex(RuntimeError,'never replay'):p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
    def test_unknown_child_refuses_and_resumes_old_worker(self):
        original=h.children
        with patch.object(h,'children',side_effect=lambda pid: original(pid)|{999999}):
            with self.assertRaisesRegex(RuntimeError,'unknown worker children'):p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
        self.assertIsNone(self.worker.poll())
        before=self.part.stat().st_size;self.wait(lambda:self.part.stat().st_size>before)
    def test_spawn_response_unknown_never_replays(self):
        with patch.object(subprocess,'Popen',side_effect=OSError('synthetic spawn failure')):
            with self.assertRaises(OSError):p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
        self.worker.wait(timeout=5)
        self.assertEqual(self.part.stat().st_ino,self.inode)
        with self.assertRaisesRegex(RuntimeError,'never replay'):p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
    def test_conflicting_predecessor_rejected(self):
        value=json.loads(Path(self.prior['audit']).read_text());value['providers']['ecmwf']={'status':'boundary_verified'};h.save_audit(Path(self.prior['audit']),value)
        with self.assertRaisesRegex(RuntimeError,'ECMWF actions'):p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
        self.assertIsNone(self.worker.poll())
    def test_partial_symlink_is_rejected_before_transfer_signal(self):
        saved=self.part.with_suffix('.saved');self.part.rename(saved);self.part.symlink_to(saved)
        try:
            with self.assertRaisesRegex(RuntimeError,'unsafe partial file'):p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
            self.assertIsNone(self.worker.poll())
            self.assertTrue(h.children(self.worker.pid))
        finally:
            self.part.unlink();saved.rename(self.part)
    def test_uncertain_child_exit_never_starts_new_worker(self):
        with patch.object(p,'exited',return_value=False),patch.object(subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError,'no force kill or restart'):p.pause(self.plan,q,self.audit/'pause.json',self.plan['digest'])
        spawn.assert_not_called();self.assertEqual(self.part.stat().st_ino,self.inode)
        self.assertEqual(json.loads((self.audit/'pause.json').read_text())['status'],'needs_review')
    def test_withdraw_active_controller_only(self):
        path=Path(self.prior['audit']);original=self.prior['plan']
        path.unlink()
        code='''import json,signal,time
from pathlib import Path
def handoff(plan,q,audit,digest):
 value={'plan':plan,'plan_digest':digest,'status':'waiting_file_boundary','providers':{'gfs':{'status':'new_worker_started'}}}
 def expired(*unused):raise TimeoutError('single-file boundary wait exhausted; worker untouched')
 signal.signal(signal.SIGALRM,expired)
 Path(audit).write_text(json.dumps(value));Path(audit).chmod(0o600)
 try:
  while True:time.sleep(.1)
 except TimeoutError as error:
  value.update(status='needs_review',error='TimeoutError: '+str(error));Path(audit).write_text(json.dumps(value))
'''
        code+='\nplan=json.loads('+repr(json.dumps(original))+');q=None\nreport=handoff(plan,q,'+repr(str(path))+','+repr(original['digest'])+')\n'
        controller=subprocess.Popen([sys.executable,'-B','-c',code]);self.addCleanup(self.stop,controller)
        self.wait(path.exists)
        receipt=self.audit/'active-withdraw.json'
        result=p.withdraw(path,original,receipt)
        self.assertEqual(result['status'],'controller_exited_no_ecmwf_action');controller.wait(timeout=5)
        self.assertIsNone(self.worker.poll());before=self.part.stat().st_size;self.wait(lambda:self.part.stat().st_size>before)
        with self.assertRaisesRegex(RuntimeError,'never replay'):p.withdraw(path,original,receipt)


if __name__=='__main__':unittest.main()
