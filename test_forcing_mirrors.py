import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_forcing_pool_queue import args, queue_module as q


CYCLE = "2026111800"
READY = b"GRIBpayload7777"


@pytest.fixture
def make_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "LOCK_ROOT", tmp_path / "locks")
    def make(source, cls=q.PoolQueue):
        pool = cls(args(tmp_path / source, source))
        pool.enqueue(CYCLE, [3], "task", "forcing_" + "a" * 32)
        return pool
    return make


def test_route_orders_and_product_paths(make_pool):
    for source, expected in (
        ("gfs", ("nomads", "noaa_aws")),
        ("ecmwf", ("ecmwf_cloudfront", "ecmwf_portal")),
    ):
        pool = make_pool(source)
        legacy, routes = pool.transfer_routes(CYCLE, 3)
        assert tuple(node for node, _ in routes) == expected
        assert dict(routes)[legacy] == pool.url(CYCLE, 3)
    pool = make_pool("ecmwf_0p1", q.SplitEcmwfQueue)
    for component, first in (("sfc", "ecmwf_portal"), ("pl", "ecmwf_cloudfront")):
        legacy, routes = pool.component_routes(CYCLE, 3, component)
        assert routes[0][0] == first
        assert legacy == "ecmwf_portal"
        assert all("/ifs/0p1/oper/" in url and url.endswith("-" + component + ".grib2") for _, url in routes)


def test_zero_byte_failure_falls_back_and_history_names_both_nodes(make_pool, monkeypatch):
    pool = make_pool("ecmwf")
    calls = []
    def transfer(command, **kwargs):
        calls.append(command[-1])
        part = Path(command[command.index("--output")+1])
        headers = Path(command[command.index("--dump-header")+1])
        if len(calls) == 1:
            return SimpleNamespace(returncode=22, stdout="0 503", stderr="HTTP 503")
        headers.write_text("HTTP/1.1 200 OK\n")
        part.write_bytes(READY)
        return SimpleNamespace(returncode=0, stdout="1000", stderr="")
    monkeypatch.setattr(q.subprocess, "run", transfer)
    pool._download_one_locked(CYCLE, 3)
    row = pool.attempt_history(CYCLE)["items"][0]
    assert row["status"] == "succeeded"
    assert [(r["transfer_node"], r["status"]) for r in row["requests"]] == [
        ("ecmwf_cloudfront", "failed"), ("ecmwf_portal", "succeeded")]
    assert pool.file_path(CYCLE, 3).read_bytes() == READY
    assert pool.load_state()["cycles"][CYCLE]["files"]["003"]["references"] == {"forcing_" + "a" * 32: True}


@pytest.mark.parametrize("new_fragment", [False, True])
def test_partial_remains_bound_to_its_original_node(make_pool, monkeypatch, new_fragment):
    pool = make_pool("ecmwf")
    calls = []
    part = Path(str(pool.file_path(CYCLE, 3)) + ".part")
    part.parent.mkdir()
    if not new_fragment:
        part.write_bytes(b"GRIB")
    def transfer(command, **kwargs):
        calls.append(command[-1])
        with part.open("ab") as out:
            out.write(b"abc" if len(calls) == 1 else b"7777")
        return SimpleNamespace(returncode=28 if len(calls) == 1 else 0, stdout="", stderr="timeout")
    monkeypatch.setattr(q.subprocess, "run", transfer)
    pool._download_one_locked(CYCLE, 3)
    assert part.read_bytes() == (b"abc" if new_fragment else b"GRIBabc")
    node = "ecmwf_cloudfront" if new_fragment else "ecmwf_portal"
    assert calls == [dict(pool.transfer_routes(CYCLE, 3)[1])[node]]
    pool._download_one_locked(CYCLE, 3)
    assert calls[0] == calls[1]
    assert len(calls) == 2


def test_changed_origin_refuses_to_touch_existing_bytes(make_pool, monkeypatch):
    pool = make_pool("ecmwf")
    part = Path(str(pool.file_path(CYCLE, 3)) + ".part")
    part.parent.mkdir()
    part.write_bytes(b"GRIBold")
    pool._set_entry(CYCLE, 3, {"transfer_node": "ecmwf_cloudfront", "transfer_url_sha256": "other"})
    monkeypatch.setattr(q.subprocess, "run", lambda *a, **k: pytest.fail("must not open a connection"))
    pool._download_one_locked(CYCLE, 3)
    assert part.read_bytes() == b"GRIBold"
