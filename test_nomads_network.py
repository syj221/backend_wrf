"""TLS fallback never replays a progressing transfer or bypasses HTTPS."""
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from test_forcing_pool_queue import queue_module, args
from hpc_transport import HpcClient

def test_nomads_zero_progress_fallback_keeps_url_resume_and_budget(tmp_path):
    q=queue_module.PoolQueue(args(tmp_path,'gfs'));p=tmp_path/'file.part';p.write_bytes(b'keep')
    cmd=['curl','--continue-at','-','--connect-timeout','30','--max-time','3600','--output',str(p),'https://nomads.ncep.noaa.gov/file']
    with patch.object(queue_module.socket,'getaddrinfo',return_value=[(2,1,6,'',('23.62.20.142',443))]), patch.object(queue_module.subprocess,'run',side_effect=[SimpleNamespace(returncode=35),SimpleNamespace(returncode=0)]) as run:
        assert q.run_curl(cmd).returncode==0
    retry=run.call_args_list[1].args[0]
    assert retry[-1]==cmd[-1] and '--resolve' in retry and '--continue-at' in retry
    assert '--insecure' not in retry and int(retry[retry.index('--max-time')+1])<=3600
    assert p.read_bytes()==b'keep'

@pytest.mark.parametrize('source,code,progress', [('ecmwf',35,False),('gfs',22,False),('gfs',60,False),('gfs',28,True)])
def test_no_fallback_on_other_provider_http_cert_or_progress(tmp_path,source,code,progress):
    q=queue_module.PoolQueue(args(tmp_path,source));p=tmp_path/'part';p.write_bytes(b'old')
    def transfer(*a,**k):
        if progress:p.write_bytes(b'oldmore')
        return SimpleNamespace(returncode=code)
    cmd=['curl','--connect-timeout','30','--max-time','3600','--output',str(p),'https://nomads.ncep.noaa.gov/file']
    with patch.object(queue_module.subprocess,'run',side_effect=transfer) as run,patch.object(queue_module.socket,'getaddrinfo') as dns:
        q.run_curl(cmd);assert run.call_count==1;dns.assert_not_called()

def test_publication_network_failure_is_not_claimed_unpublished():
    d=HpcClient._queue_cycle_details({'cycles':{'2026101900':{'publication':'waiting_publish','publication_error':'curl: (28) timed out secret','files':{'000':{'status':'queued'}}}}},'2026101900')
    assert '暂无法核验' in d['wait_reason'] and 'secret' not in d['wait_reason']

def test_no_fallback_after_original_time_budget(tmp_path):
    q=queue_module.PoolQueue(args(tmp_path,'gfs'));cmd=['curl','--connect-timeout','20','--max-time','45','--output',str(tmp_path/'probe'),'https://nomads.ncep.noaa.gov/file']
    with patch.object(queue_module.time,'monotonic',side_effect=[0,46]),patch.object(queue_module.socket,'getaddrinfo',return_value=[(2,1,6,'',('23.62.20.142',443))]),patch.object(queue_module.subprocess,'run',return_value=SimpleNamespace(returncode=28)) as run:
        q.run_curl(cmd);assert run.call_count==1

def test_dns_lookup_failure_preserves_original_result(tmp_path):
    q=queue_module.PoolQueue(args(tmp_path,'gfs'));cmd=['curl','--connect-timeout','20','--max-time','45','--output',str(tmp_path/'probe'),'https://nomads.ncep.noaa.gov/file'];result=SimpleNamespace(returncode=35)
    with patch.object(queue_module.socket,'getaddrinfo',side_effect=OSError('DNS unavailable')),patch.object(queue_module.subprocess,'run',return_value=result):
        assert q.run_curl(cmd) is result
