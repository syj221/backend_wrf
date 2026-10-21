"""Native Linux tests; dedicated disposable roots, no production queue changes."""
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

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import handoff_pool_workers as h
import forcing_pool_queue as q

FIXTURE = r'''
import argparse, importlib.util, json, os, subprocess, sys, time
from pathlib import Path
spec=importlib.util.spec_from_file_location('fixture_queue',os.environ['HANDOFF_QUEUE_MODULE']);q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)
p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--source');p.add_argument('--root');a=p.parse_args()
a.min_bytes=8;a.min_speed=1;a.request_interval=0;a.retry_max_attempts=8;a.retry_max_seconds=900;a.publish_probe_seconds=300;a.history_retention_days=30;a.ec_template=''
queue=q.PoolQueue(a);root=Path(a.root);cycle='2026101600'
if 'old.py' in sys.argv[0]:
 with q.exclusive_process(queue.worker_lock) as worker:
  if worker is None:sys.exit(3)
  if a.source=='ecmwf':
   with q.kernel_guard(root/'.pool-download-slot' if os.environ.get('HANDOFF_MODERN_FIXTURE') else root.parent/'.pool-download-slot') as transfer:
    with q.exclusive(queue.cycle_lock(cycle)):
     queue._set_entry(cycle,0,{'status':'downloading','current_file':queue.file_name(0)})
     path=queue.file_path(cycle,0);path.parent.mkdir(exist_ok=True)
     child="import sys,time;from pathlib import Path;p=Path(sys.argv[1]);p.write_bytes(b'GRIB');time.sleep(3);p.write_bytes(b'GRIBold-complete7777')"
     subprocess.run([sys.executable,'-c',child,str(path)],pass_fds=(worker,transfer),check=True)
     queue._set_entry(cycle,0,{'status':'ready','bytes':path.stat().st_size})
  while True:
   with q.exclusive(queue.state_lock):pass
   time.sleep(.02)
else:
 def write(cycle,hour):
  path=queue.file_path(cycle,hour);part=Path(str(path)+'.part');path.parent.mkdir(exist_ok=True)
  queue._set_entry(cycle,hour,{'status':'downloading','current_file':queue.file_name(hour)})
  with (root/'intervals.jsonl').open('a') as log:log.write(json.dumps({'event':'start','time':time.time(),'hour':hour})+'\n')
  with part.open('ab') as out:out.write(b'GRIB' if out.tell()==0 else b'');out.flush();time.sleep(2);out.write(b'new-complete7777')
  os.replace(part,path);queue._set_entry(cycle,hour,{'status':'ready','bytes':path.stat().st_size})
  with (root/'intervals.jsonl').open('a') as log:log.write(json.dumps({'event':'end','time':time.time(),'hour':hour})+'\n')
 queue._download_one_locked=write;sys.exit(queue.worker())
'''

@unittest.skipUnless(sys.platform == 'linux', 'requires native Linux pidfd')
class PoolHandoffTest(unittest.TestCase):
    def setUp(self):
        parent=os.environ.get('HANDOFF_TEST_DATA_ROOT')
        self.tmp=tempfile.TemporaryDirectory(prefix='.pool-handoff-test-',dir=parent);self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.old=self.base/'old.py';self.new=self.base/'new.py'
        self.old.write_text(FIXTURE);self.new.write_text(FIXTURE+'\n# new fixture\n')
        self.roots={s:str(self.base/s) for s in ('gfs','ecmwf')};self.workers=[]
        module=os.environ.get('HANDOFF_QUEUE_MODULE',str(Path(q.__file__).resolve()))
        for source,root in self.roots.items():
            pool=q.PoolQueue(type('Args',(),{'source':source,'root':root,'min_bytes':8})())
            hours=[0,1,2] if source=='gfs' else [0,3,6]
            pool.enqueue('2026101600',hours,'task','forcing_'+'a'*32)
            if source=='gfs':
                path=pool.file_path('2026101600',0);path.parent.mkdir(exist_ok=True);path.write_bytes(b'GRIBexisting7777')
                pool._set_entry('2026101600',0,{'status':'ready','bytes':path.stat().st_size})
            part=Path(str(pool.file_path('2026101600',hours[1]))+'.part');part.parent.mkdir(exist_ok=True);part.write_bytes(b'GRIBpartial-prefix')
            (Path(root)/'logs').mkdir(exist_ok=True)
            proc=subprocess.Popen([sys.executable,str(self.old),'worker','--source',source,'--root',root],env={**os.environ,'HANDOFF_QUEUE_MODULE':module})
            self.workers.append(proc)
        self.addCleanup(self.stop_samples)
        end=time.monotonic()+5
        while time.monotonic()<end:
            try:
                if all((Path(r)/'.pool-worker.lockdir/owner.json').exists() for r in self.roots.values()) and h.children(self.workers[1].pid):break
            except FileNotFoundError:pass
            time.sleep(.02)
        else:self.fail('sample workers did not start')
        self.plan=h.prepare(self.roots,str(self.old),str(self.new));audit_tmp=tempfile.TemporaryDirectory(prefix='pool-handoff-audit-');self.addCleanup(audit_tmp.cleanup);self.audit=Path(audit_tmp.name)/'handoff.json'
        self.environment={**os.environ,'HANDOFF_QUEUE_MODULE':module};self.env_old=os.environ.get('HANDOFF_QUEUE_MODULE');os.environ['HANDOFF_QUEUE_MODULE']=module
        self.addCleanup(self.restore_env)
    def restore_env(self):
        if self.env_old is None:os.environ.pop('HANDOFF_QUEUE_MODULE',None)
        else:os.environ['HANDOFF_QUEUE_MODULE']=self.env_old
    def stop_samples(self):
        # Allow the owned transfer children to finish before fixture teardown.
        time.sleep(5)
        for proc in self.workers:
            if proc.poll() is None:proc.terminate()
        for proc in self.workers:proc.wait(timeout=5)
        # New fixture workers naturally finish within three seconds.
        time.sleep(5)
    def test_boundary_handoff_preserves_files_partials_references_and_overlap(self):
        start=time.time();report=h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=12)
        self.assertEqual(report['status'],'new_workers_started')
        self.assertLess(report['providers']['gfs']['boundary_at']-start,2)
        self.assertGreater(report['providers']['ecmwf']['boundary_at']-start,2)
        self.assertEqual((Path(self.roots['ecmwf'])/'2026101600/ecmwf.t00z.ifs.0p25.f000.grib2').read_bytes(),b'GRIBold-complete7777')
        deadline=time.monotonic()+12
        while time.monotonic()<deadline and any((Path(r)/'.pool-worker.lockdir').exists() for r in self.roots.values()):time.sleep(.05)
        spans=[]
        for source,root in self.roots.items():
            root=Path(root);state=json.loads((root/'.pool-queue.json').read_text());self.assertEqual(state['transport_policy'],'per-provider-1')
            self.assertTrue(all(e['status']=='ready' for e in state['cycles']['2026101600']['files'].values()))
            for key, entry in state['cycles']['2026101600']['files'].items():
                self.assertEqual(entry.get('references'),report['providers'][source]['queue']['cycles']['2026101600']['files'][key].get('references'))
            name='gfs.t00z.pgrb2.0p25.f001' if source=='gfs' else 'ecmwf.t00z.ifs.0p25.f003.grib2'
            self.assertTrue((root/'2026101600'/name).read_bytes().startswith(b'GRIBpartial-prefix'))
            rows=[json.loads(line) for line in (root/'intervals.jsonl').read_text().splitlines()];spans.append((rows[0]['time'],rows[-1]['time']))
            with q.kernel_guard(root/'.pool-worker.lockdir',blocking=False) as gate:self.assertIsNotNone(gate)
            with q.kernel_guard(root/'.pool-download-slot',blocking=False) as gate:self.assertIsNotNone(gate)
        # GFS starts immediately while the old ECMWF child is still finishing.
        self.assertLess(spans[0][0],report['providers']['ecmwf']['boundary_at'])
        self.assertLess(max(span[0] for span in spans), min(span[1] for span in spans))
        with self.assertRaisesRegex(RuntimeError,'never replay'):h.handoff(self.plan,q,self.audit,self.plan['digest'])
    def test_timeout_leaves_active_file_and_pending_worker_untouched(self):
        with self.assertRaises(TimeoutError):h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=.3)
        self.assertIsNone(self.workers[1].poll())
        self.assertTrue(h.children(self.workers[1].pid))
        self.assertEqual(json.loads(self.audit.read_text())['status'],'needs_review')
    def test_companion_fence_waits_for_current_file_and_original_controller(self):
        helper=self.base/'fixture_handoff.py'
        helper.write_text(getattr(h,'__source__',Path(h.__file__).read_text() if hasattr(h,'__file__') else ''))
        module=os.environ['HANDOFF_QUEUE_MODULE']
        code="import sys,json,importlib.util;sys.path.insert(0,"+repr(str(self.base))+");from fixture_handoff import handoff;spec=importlib.util.spec_from_file_location('fence_queue',"+repr(module)+");q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q);plan=json.loads("+repr(json.dumps(self.plan))+")\n"
        code+='report=handoff(plan,q,'+repr(str(self.audit))+','+repr(self.plan['digest'])+',timeout=12)\n'
        controller=subprocess.Popen([sys.executable,'-B','-c',code],stdout=subprocess.DEVNULL)
        self.addCleanup(controller.wait,timeout=20)
        end=time.monotonic()+5
        while time.monotonic()<end:
            if self.audit.exists():
                record=json.loads(self.audit.read_text())
                if record.get('status')=='waiting_file_boundary' and record.get('providers',{}).get('gfs',{}).get('status')=='new_worker_started':break
            time.sleep(.02)
        value=h.fence_existing_handoff(self.plan,q,self.audit,self.plan['digest'],timeout=12)
        controller.wait(timeout=5)
        self.assertEqual(controller.returncode,0)
        self.assertEqual(value['status'],'released_after_handoff')
        self.assertGreater(value['acquired_at']-value['started_at'],1)
        self.assertEqual(json.loads(self.audit.read_text())['status'],'new_workers_started')

    def test_spawn_failure_is_audited_and_never_replayed(self):
        from unittest.mock import patch
        with patch.object(h.subprocess, 'Popen', side_effect=OSError('synthetic spawn response lost')):
            with self.assertRaises(OSError):h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=10)
        record=json.loads(self.audit.read_text())
        self.assertEqual(record['status'],'needs_review')
        self.assertEqual(record['providers']['gfs']['status'],'old_worker_exited')
        self.assertIsNone(self.workers[1].poll())
        with self.assertRaisesRegex(RuntimeError,'never replay'):h.handoff(self.plan,q,self.audit,self.plan['digest'])

    def test_last_file_manifest_pending_is_not_interrupted(self):
        root=Path(self.roots['gfs']);path=root/'.pool-queue.json'
        with q.exclusive(root/'.pool-queue.lock'):
            state=json.loads(path.read_text());state['cycles']['2026101600']['files']={f'{hour:03d}':{'status':'ready'} for hour in range(73)};q.atomic_json(path,state)
        with self.assertRaises(TimeoutError):h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=.3)
        self.assertTrue(all(proc.poll() is None for proc in self.workers))
        self.assertEqual(json.loads(self.audit.read_text())['providers'],{})

    def test_gfs_provider_slot_timeout_preserves_worker_and_partial(self):
        root=Path(self.roots['gfs']);before=h.identity(self.workers[0].pid)
        single=h.prepare({'gfs':str(root)},str(self.old),str(self.new))
        program="import importlib.util,sys,time;from pathlib import Path;s=importlib.util.spec_from_file_location('q',sys.argv[1]);q=importlib.util.module_from_spec(s);s.loader.exec_module(q);lock=q.kernel_guard(Path(sys.argv[2])/'.pool-download-slot',blocking=True);lock.__enter__();print('locked',flush=True);time.sleep(2);lock.__exit__(None,None,None)"
        holder=subprocess.Popen([sys.executable,'-c',program,os.environ['HANDOFF_QUEUE_MODULE'],str(root)],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(),'locked')
            with self.assertRaises(TimeoutError):
                h.handoff(single,q,self.audit,single['digest'],timeout=.2)
            h.check_identity(before)
            part=root/'2026101600/gfs.t00z.pgrb2.0p25.f001.part'
            self.assertEqual(part.read_bytes(),b'GRIBpartial-prefix')
        finally:
            holder.wait(timeout=5)
            holder.stdout.close()

    def test_gfs_only_handoff_preserves_ecmwf_identity_and_partial(self):
        other=h.identity(self.workers[1].pid)
        part=Path(self.roots['gfs'])/'2026101600/gfs.t00z.pgrb2.0p25.f001.part'
        inode=part.stat().st_ino
        single=h.prepare({'gfs':self.roots['gfs']},str(self.old),str(self.new))
        record=h.handoff(single,q,self.audit,single['digest'],timeout=12)
        self.assertEqual(record['status'],'new_workers_started')
        self.assertEqual(set(record['providers']),{'gfs'})
        h.check_identity(other)
        final=Path(str(part).removesuffix('.part'))
        deadline=time.monotonic()+8
        while not final.exists() and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(final.stat().st_ino,inode)
        self.assertTrue(final.read_bytes().startswith(b'GRIBpartial-prefix'))

    def test_ecmwf_only_handoff_preserves_other_source(self):
        single=h.prepare({'ecmwf':self.roots['ecmwf']},str(self.old),str(self.new))
        record=h.handoff(single,q,self.audit,single['digest'],timeout=12)
        self.assertEqual(set(record['providers']),{'ecmwf'})
        self.assertIsNone(self.workers[0].poll())
        self.assertEqual((Path(self.roots['ecmwf'])/'2026101600/ecmwf.t00z.ifs.0p25.f000.grib2').read_bytes(),b'GRIBold-complete7777')
        with q.kernel_guard(self.base/'.pool-download-slot',blocking=False) as gate:self.assertIsNotNone(gate)
    def test_ecmwf_only_timeout_preserves_current_file(self):
        single=h.prepare({'ecmwf':self.roots['ecmwf']},str(self.old),str(self.new))
        with self.assertRaises(TimeoutError):h.handoff(single,q,self.audit,single['digest'],timeout=.2)
        self.assertTrue(all(proc.poll() is None for proc in self.workers))
        self.assertTrue(h.children(self.workers[1].pid))
    def test_single_file_minimum_budget_is_enforced(self):
        single=h.prepare({'ecmwf':self.roots['ecmwf']},str(self.old),str(self.new));single.pop('digest');single['minimum_wait_seconds']=4200;single['digest']=h.digest(single)
        with self.assertRaisesRegex(RuntimeError,'single-file limit'):h.handoff(single,q,self.audit,single['digest'],timeout=3600)
        self.assertTrue(all(proc.poll() is None for proc in self.workers))
    def test_predecessor_proof_rejects_any_ecmwf_action_and_preserves_audit(self):
        single=h.prepare({'ecmwf':self.roots['ecmwf']},str(self.old),str(self.new));prior=self.audit.parent/'prior.json'
        value={'plan':self.plan,'plan_digest':self.plan['digest'],'status':'needs_review','error':'TimeoutError: file boundary wait exhausted; remaining workers untouched','providers':{'gfs':{'status':'new_worker_started'}}}
        h.save_audit(prior,value);h.save_audit(prior.with_name('prior.fence.json'),{'plan_digest':self.plan['digest'],'status':'released_needs_review','controller':{'pid':2147483647}})
        envelope={'plan':self.plan,'audit':str(prior)};original=prior.read_bytes()
        proof=h.verify_unacted_ecmwf(envelope,single)
        self.assertFalse(proof['ecmwf_actions']);self.assertEqual(prior.read_bytes(),original)
        value['providers']['ecmwf']={'status':'stopping_idle_worker'};h.save_audit(prior,value)
        with self.assertRaisesRegex(RuntimeError,'unknown or ECMWF actions'):h.verify_unacted_ecmwf(envelope,single)
        self.assertTrue(all(proc.poll() is None for proc in self.workers))

    def test_changed_identity_is_rejected_without_signal(self):
        self.plan['workers'][0]['process']['start_ticks']='0';self.plan.pop('digest');self.plan['digest']=h.digest(self.plan)
        with self.assertRaisesRegex(RuntimeError,'identity changed'):h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=1)
        self.assertTrue(all(proc.poll() is None for proc in self.workers))
    def test_changed_module_is_rejected_without_signal(self):
        self.new.write_text('changed')
        with self.assertRaisesRegex(RuntimeError,'module changed'):h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=1)
        self.assertTrue(all(proc.poll() is None for proc in self.workers))

if __name__=='__main__':unittest.main()


@unittest.skipUnless(sys.platform == 'linux', 'requires native Linux pidfd')
class ModernPoolHandoffTest(unittest.TestCase):
    stop_samples = PoolHandoffTest.stop_samples
    restore_env = PoolHandoffTest.restore_env
    def setUp(self):
        self.previous_modern=os.environ.get('HANDOFF_MODERN_FIXTURE')
        os.environ['HANDOFF_MODERN_FIXTURE']='1'
        self.addCleanup(self.restore_modern)
        PoolHandoffTest.setUp(self)
        self.plan.pop('digest');self.plan['source_transfer_fences']=True;self.plan['digest']=h.digest(self.plan)
    def restore_modern(self):
        if self.previous_modern is None:os.environ.pop('HANDOFF_MODERN_FIXTURE',None)
        else:os.environ['HANDOFF_MODERN_FIXTURE']=self.previous_modern
    def test_modern_two_provider_handoff_waits_for_live_child(self):
        report=h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=12)
        self.assertEqual(report['status'],'new_workers_started')
        self.assertEqual(set(report['providers']),{'gfs','ecmwf'})
        self.assertEqual((Path(self.roots['ecmwf'])/'2026101600/ecmwf.t00z.ifs.0p25.f000.grib2').read_bytes(),b'GRIBold-complete7777')
    def test_modern_timeout_preserves_both_original_processes(self):
        identities=[h.identity(p.pid) for p in self.workers]
        with self.assertRaises(TimeoutError):h.handoff(self.plan,q,self.audit,self.plan['digest'],timeout=.2)
        for item in identities:h.check_identity(item)
        self.assertEqual(json.loads(self.audit.read_text())['status'],'needs_review')
