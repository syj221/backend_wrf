"""The scheduler and transport share the existing source contract."""
import pytest
from gfs import GfsManager, normalize_source
from hpc_transport import HpcClient

@pytest.mark.parametrize('value,expected', [(None,'gfs'),('','gfs'),(' GFS ','gfs'),('ec','ecmwf'),(' ECMWF ','ecmwf')])
def test_both_entrypoints_use_shared_normalizer(value, expected):
    assert GfsManager.normalize_source is normalize_source
    assert HpcClient._normalize_forcing_source is normalize_source
    assert normalize_source(value) == expected

@pytest.mark.parametrize('value', ['unknown', 'ecmwf;rm', 'gfs/../ec'])
def test_invalid_sources_still_fail(value):
    with pytest.raises(ValueError, match='数据源必须'):
        normalize_source(value)
