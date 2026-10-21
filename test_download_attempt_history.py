"""Persistent attempt evidence, retry compatibility and read-only paging."""
import ast
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
import pytest
from test_forcing_pool_queue import queue_module as q, args
from hpc_transport import HpcClient
from config import Settings

@pytest.fixture
def pool(tmp_path,monkeypatch):
    monkeypatch.setattr(q,'LOCK_ROOT',tmp_path/'locks')
    return q.PoolQueue(args(tmp_path,'ecmwf'))

@pytest.mark.parametrize('code,http,error,elapsed,budget,cause',[
 (28,200,'Operation too slow. Less than 1024 bytes/sec transferred the last 120 seconds',121,3600,'low_speed_timeout'),
 (28,206,'Operation timed out',3600,3600,'total_timeout'),
 (28,0,'SSL connection timeout',30,3600,'connection_failure'),
 (35,0,'SSL_ERROR_SYSCALL',2,3600,'connection_failure'),
 (22,429,'HTTP 429',1,3600,'rate_limited'),
 (22,503,'HTTP 503',1,3600,'upstream_temporary'),
 (28,200,'unexpected timeout',200,3600,'timeout_unknown')])
def test_error_causes_are_evidence_based(code,http,error,elapsed,budget,cause):
    assert q.PoolQueue.attempt_cause(code,http,error,elapsed,budget)==cause

def test_failed_attempt_survives_success_and_counter_reset(pool):
    cycle='2026102000';pool.enqueue(cycle,[0],'task','forcing_'+'a'*32)
    part=Path(str(pool.file_path(cycle,0))+'.part');part.parent.mkdir(exist_ok=True);part.write_bytes(b'GRIBpartial')
    inode=part.stat().st_ino
    def failed(command,**kwargs):
        Path(str(part)+'.headers').write_text('HTTP/1.1 200 OK\r\n')
        with part.open('ab') as f:f.write(b'more')
        return SimpleNamespace(returncode=28,stdout='2048',stderr='Operation too slow. Less than 1024 bytes/sec transferred the last 120 seconds secret /private/path')
    with patch.object(q.subprocess,'run',side_effect=failed):pool._download_one_locked(cycle,0)
    first=pool.attempt_history(cycle)['items'][0]
    assert first['cause']=='low_speed_timeout' and first['requests'][0]['cause']=='low_speed_timeout'
    pool._set_entry(cycle,0,{'attempts':0,'retry_at':0})
    def success(command,**kwargs):
        Path(str(part)+'.headers').write_text('HTTP/1.1 206 Partial Content\r\n')
        with part.open('ab') as f:f.write(b'7777')
        return SimpleNamespace(returncode=0,stdout='10000',stderr='')
    with patch.object(q.subprocess,'run',side_effect=success):pool._download_one_locked(cycle,0)
    rows=pool.attempt_history(cycle)['items']
    assert len(rows)==2 and rows[0]['id']!=rows[1]['id']
    assert rows[0]['status']=='succeeded' and rows[1]['cause']=='low_speed_timeout'
    assert pool.file_path(cycle,0).stat().st_ino==inode
    assert pool.load_state()['cycles'][cycle]['files']['000']['status']=='ready'
    assert 'secret' not in json.dumps(rows) and '/private/path' not in json.dumps(rows)
    assert pool.load_state()['cycles'][cycle]['files']['000']['references']=={'forcing_'+'a'*32:True}

def test_paging_success_history_missing_legacy_and_torn_records(pool):
    cycle='2026102000';assert pool.attempt_history(cycle)['legacy_unrecorded']
    for i in range(5):
        pool.record_attempt(cycle,{'event':'start','id':str(i),'file':'f000','attempt':i})
        pool.record_attempt(cycle,{'event':'finish','id':str(i),'status':'succeeded'})
    first=pool.attempt_history(cycle,limit=2);assert [r['id'] for r in first['items']]==['4','3']
    second=pool.attempt_history(cycle,before=first['next_cursor'],limit=2);assert [r['id'] for r in second['items']]==['2','1']
    final=pool.attempt_history(cycle,before=second['next_cursor'],limit=2);assert [r['id'] for r in final['items']]==['0'] and final['next_cursor'] is None
    with (pool.root/'.pool-attempts'/f'{cycle}.jsonl').open('ab') as f:f.write(b'{"partial')
    pool.record_attempt(cycle,{'event':'start','id':'interrupted','file':'f003','attempt':1})
    result=pool.attempt_history(cycle);assert result['damaged_records']==1 and result['items'][0]['status']=='unfinished'

@pytest.mark.parametrize('bad', ['../2026102000','2026102000;echo secret','not-cycle'])
def test_scope_rejects_paths_before_io(pool,bad):
    with pytest.raises(ValueError):pool.attempt_history(bad)

def test_history_symlink_is_never_followed(pool,tmp_path):
    outside=tmp_path/'preserve';outside.write_text('preserve')
    (tmp_path/'.pool-attempts').mkdir();(tmp_path/'.pool-attempts/2026102000.jsonl').symlink_to(outside)
    with pytest.raises(OSError):pool.record_attempt('2026102000',{'id':'a','event':'start'})
    assert outside.read_text()=='preserve'

def test_history_failure_prevents_download(pool):
    pool.enqueue('2026102000',[0],'task')
    with patch.object(pool,'record_attempt',side_effect=RuntimeError('audit full')),patch.object(q.subprocess,'run') as run:
        with pytest.raises(RuntimeError):pool._download_one_locked('2026102000',0)
        run.assert_not_called()

def test_transport_failure_then_success_is_not_lost_inside_gfs_retry(pool):
    pool.source='gfs';cycle='2026102000';pool.enqueue(cycle,[0],'task')
    part=Path(str(pool.file_path(cycle,0))+'.part')
    def transfer(command,**kwargs):
        if '--resolve' not in command:return SimpleNamespace(returncode=35,stdout='',stderr='TLS connection failed')
        part.write_bytes(b'GRIBpayload7777');return SimpleNamespace(returncode=0,stdout='100',stderr='')
    with patch.object(q.subprocess,'run',side_effect=transfer),patch.object(q.socket,'getaddrinfo',return_value=[(2,1,6,'',('1.2.3.4',443))]):pool._download_one_locked(cycle,0)
    row=pool.attempt_history(cycle)['items'][0]
    assert row['status']=='succeeded' and [r['cause'] for r in row['requests']]==['connection_failure','']

def test_client_validates_scope_and_redacts_unknown_fields_before_return():
    h=HpcClient(Settings());h.run=Mock(return_value=json.dumps({'items':[{'id':'a','file':'f000','raw_error':'Bearer secret','requests':[{'request':1,'cause':'rate_limited','url':'private'}]}]}))
    with pytest.raises(ValueError):h.forcing_attempt_history('gfs','../bad')
    h.run.assert_not_called()
    value=h.forcing_attempt_history('gfs','2026102000');assert 'secret' not in json.dumps(value) and 'private' not in json.dumps(value)

def test_real_route_query_limits_and_authentication():
    from fastapi import FastAPI,Query,HTTPException
    from fastapi.testclient import TestClient
    from typing import Literal
    from auth import install_auth
    tree=ast.parse((Path(__file__).parent/'main.py').read_text());route=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='forcing_attempt_history')
    h=Mock();h.forcing_attempt_history.return_value={'items':[],'next_cursor':None}
    app=FastAPI();scope={'app':app,'hpc_client':h,'Literal':Literal,'Query':Query,'HTTPException':HTTPException,'ok':lambda value:{'code':0,'data':value}}
    exec(compile(ast.Module(body=[route],type_ignores=[]),'real-history-route','exec'),scope)
    install_auth(app,[('/api/wrf/forcing',{'GET':1})])
    client=TestClient(app)
    assert client.get('/api/wrf/forcing/gfs/cycles/2026102000/attempts').status_code==401
    h.forcing_attempt_history.assert_not_called()
    plain=FastAPI();scope['app']=plain;exec(compile(ast.Module(body=[route],type_ignores=[]),'real-history-route','exec'),scope);client=TestClient(plain)
    assert client.get('/api/wrf/forcing/gfs/cycles/2026102000/attempts?limit=101').status_code==422
    assert client.get('/api/wrf/forcing/gfs/cycles/2026102000/attempts?before=-1').status_code==422
    assert client.get('/api/wrf/forcing/ecmwf/cycles/2026102000/attempts').status_code==200
