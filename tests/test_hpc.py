from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import uuid
import pytest

from config import settings
from hpc import HpcAuthError, HpcClient, HpcError, write_task_bundle
from hpc_transport import GFS_PRODUCT, GFS_SCOPE, HpcSessionStaleError


def test_hpc_health_requires_runtime_directories(monkeypatch) -> None:
    client = HpcClient(replace(settings, hpc_remote_dir="~/WRFwork", hpc_gfs_dir="~/Data/gfsdata"))
    monkeypatch.setattr(client, "run", lambda *_args, **_kwargs: "缺少: WPS_GEOG")
    assert client.health()["status"] == "unavailable"
    monkeypatch.setattr(client, "run", lambda *_args, **_kwargs: "WRF_HPC_READY")
    assert client.health()["status"] == "ready"


def test_gfs_pool_marks_only_safe_old_cycles_for_cleanup(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(
        client,
        "run",
        lambda *_args, **_kwargs: "\n".join(
            [
                "ROOT|/share/home/user/Data/gfsdata",
                "2026072100|ready|73|1000|072",
                "2026072000|ready|73|900|072",
                "2026071900|ready|73|800|072",
                "2026071800|downloading|20|200|019",
                "2026071700|ready|73|700|072",
            ]
        ),
    )

    item = client.gfs_pool_items(
        ["2026072100", "2026072000"],
        {"2026071700"},
    )[0]
    cycles = {cycle["cycle"]: cycle for cycle in item["cycles"]}

    assert item["targets_complete"] is True
    assert item["cleanup_candidates"] == ["/share/home/user/Data/gfsdata/2026071900"]
    assert cycles["2026071800"]["cleanup_allowed"] is False
    assert cycles["2026071700"]["protected"] is True


def test_gfs_pool_blocks_cleanup_until_both_targets_are_complete(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(
        client,
        "run",
        lambda *_args, **_kwargs: "\n".join(
            [
                "ROOT|/share/home/user/Data/gfsdata",
                "2026072100|downloading|14|100|013",
                "2026072000|ready|73|900|072",
                "2026071900|ready|73|800|072",
            ]
        ),
    )

    item = client.gfs_pool_items(["2026072100", "2026072000"], set())[0]

    assert item["targets_complete"] is False
    assert item["cleanup_candidates"] == []


def test_gfs_pool_keeps_missing_target_and_exposes_remote_error(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(
        client,
        "run",
        lambda *_args, **_kwargs: "\n".join(
            [
                "ROOT|/share/home/user/Data/gfsdata",
                "2026072100|error|0|0|0|NOAA cycle is not available",
                "2026072000|ready|73|900|072|download complete",
            ]
        ),
    )

    item = client.gfs_pool_items(["2026072100", "2026072000"], set())[0]
    cycles = {cycle["cycle"]: cycle for cycle in item["cycles"]}

    assert item["status"] == "error"
    assert cycles["2026072100"]["target"] is True
    assert cycles["2026072100"]["download_message"] == "NOAA cycle is not available"
    assert cycles["2026072100"]["remote_path"].endswith("/2026072100")


def test_trigger_gfs_download_reports_immediate_remote_failure(monkeypatch) -> None:
    client = HpcClient(settings)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return "FAILED|curl: (28) NOAA request timed out"

    monkeypatch.setattr(client, "run", run)

    result = client.trigger_gfs_download("2026072100", 72)

    assert result == {
        "cycle": "2026072100",
        "status": "failed",
        "detail": "curl: (28) NOAA request timed out",
    }
    assert "kill -0" in commands[0]
    assert "download_2026072100.out" in commands[0]
    assert "download_gfs_00z.sh 20260721" in commands[0]
    assert "download_gfs_00z.sh 2026072100 72" not in commands[0]


def test_cleanup_gfs_cycles_requires_exact_allowlisted_path(monkeypatch) -> None:
    client = HpcClient(settings)
    allowed = "/share/home/user/Data/gfsdata/2026071900"
    monkeypatch.setattr(
        client,
        "gfs_pool_items",
        lambda *_args, **_kwargs: [{
            "remote_root": "/share/home/user/Data/gfsdata",
            "cleanup_candidates": [allowed],
        }],
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return f"DELETED|{allowed}"

    monkeypatch.setattr(client, "run", run)

    assert client.cleanup_gfs_cycles(
        [allowed], ["2026072100", "2026072000"], set()
    ) == {"deleted": [allowed], "missing": []}
    assert "rm -rf -- /share/home/user/Data/gfsdata/2026071900" in commands[0]

    with pytest.raises(ValueError, match="不允许清理"):
        client.cleanup_gfs_cycles(
            ["/tmp/2026071900"], ["2026072100", "2026072000"], set()
        )


def test_hpc_permission_error_requires_interactive_auth(monkeypatch) -> None:
    client = HpcClient(settings)

    def deny(*_args, **_kwargs):
        raise HpcAuthError("超算密码认证失败")

    monkeypatch.setattr(client, "run", deny)
    result = client.health()
    assert result["status"] == "auth_required"
    assert result["message"] == "需要输入超算登录密码"
    assert result["connection_mode"] == "bastion"


def test_hpc_password_is_kept_only_for_ready_process_session(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(client, "health", lambda: {"status": "ready", "message": "WRF_HPC_READY"})

    assert client.authenticate_password("session-secret")["status"] == "ready"
    assert client._password() == "session-secret"
    client.clear_session_password()
    assert client._session_password is None


def test_bastion_session_navigates_menus_once_and_is_reused(monkeypatch) -> None:
    class FakeChild:
        def __init__(self):
            self.indices = iter([0, 0, 0, 0])
            self.sent = []
            self.delaybeforesend = 0.05

        def expect_exact(self, *_args, **_kwargs):
            return next(self.indices)

        def sendline(self, value):
            self.sent.append(value)

        def isalive(self):
            return True

        def eof(self):
            return False

        def close(self, **_kwargs):
            pass

    child = FakeChild()

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()
        spawn_count = 0
        spawn_args = None

        @classmethod
        def spawn(cls, *args, **kwargs):
            cls.spawn_count += 1
            cls.spawn_args = (args, kwargs)
            return child

    cfg = replace(settings, hpc_auth_mode="password", hpc_password="session-secret")
    client = HpcClient(cfg)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)

    with client._session_lock:
        client._connect_session_locked()
        client._connect_session_locked()

    assert FakePexpect.spawn_count == 1
    assert child.sent == ["session-secret", "4", "2"]
    args, _kwargs = FakePexpect.spawn_args
    assert args[0] == "/bin/bash"
    assert args[1][0] == "-c"
    assert args[1][1].endswith(" chaosuan")
    assert "xjm_shaoyongjin@chaosuan" not in args[1][1]


def test_bastion_can_enter_an_already_selected_shell(monkeypatch) -> None:
    class FakeChild:
        def __init__(self):
            self.indices = iter([0, 1])
            self.sent = []
            self.before = ""
            self.after = ""
            self.delaybeforesend = 0.05

        def expect_exact(self, *_args, **_kwargs):
            return next(self.indices)

        def sendline(self, value):
            self.sent.append(value)

        def isalive(self):
            return True

        def eof(self):
            return False

        def close(self, **_kwargs):
            pass

    child = FakeChild()

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()

        @classmethod
        def spawn(cls, *_args, **_kwargs):
            return child

    cfg = replace(
        settings,
        hpc_auth_mode="password",
        hpc_password="session-secret",
        hpc_connect_retries=1,
    )
    client = HpcClient(cfg)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)

    with client._session_lock:
        client._connect_session_locked()

    assert child.sent == ["session-secret"]
    assert client.connection_diagnostic["stage"] == "ready"


def test_shell_initialization_reconnects_after_node_disconnect(monkeypatch) -> None:
    class FakeChild:
        def __init__(self, indices):
            self.indices = iter(indices)
            self.sent = []
            self.before = "Connection to compute node closed"
            self.after = ""
            self.delaybeforesend = 0.05
            self.closed = False

        def expect_exact(self, *_args, **_kwargs):
            return next(self.indices)

        def sendline(self, value):
            self.sent.append(value)

        def isalive(self):
            return not self.closed

        def eof(self):
            return self.closed

        def close(self, **_kwargs):
            self.closed = True

    children = [FakeChild([0, 0, 0, 5]), FakeChild([0, 0, 0, 0])]

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()
        spawn_count = 0

        @classmethod
        def spawn(cls, *_args, **_kwargs):
            child = children[cls.spawn_count]
            cls.spawn_count += 1
            return child

    cfg = replace(
        settings,
        hpc_auth_mode="password",
        hpc_password="session-secret",
        hpc_connect_retries=2,
        hpc_connect_retry_delay_seconds=0,
    )
    client = HpcClient(cfg)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)

    with client._session_lock:
        client._connect_session_locked()

    assert FakePexpect.spawn_count == 2
    assert client.connection_diagnostic["stage"] == "ready"
    assert client.connection_diagnostic["attempt"] == 2


def test_authentication_failure_is_not_retried(monkeypatch) -> None:
    class FakeChild:
        before = "Permission denied"
        after = ""
        delaybeforesend = 0.05

        def __init__(self):
            self.indices = iter([0, 6])

        def expect_exact(self, *_args, **_kwargs):
            return next(self.indices)

        def sendline(self, _value):
            pass

        def isalive(self):
            return True

        def eof(self):
            return False

        def close(self, **_kwargs):
            pass

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()
        spawn_count = 0

        @classmethod
        def spawn(cls, *_args, **_kwargs):
            cls.spawn_count += 1
            return FakeChild()

    cfg = replace(
        settings,
        hpc_auth_mode="password",
        hpc_password="session-secret",
        hpc_connect_retries=3,
    )
    client = HpcClient(cfg)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)

    with pytest.raises(HpcAuthError):
        with client._session_lock:
            client._connect_session_locked()

    assert FakePexpect.spawn_count == 1


def test_compute_node_may_request_same_password_after_account_menu(monkeypatch) -> None:
    class FakeChild:
        def __init__(self):
            self.indices = iter([0, 0, 0, 2, 0])
            self.sent = []
            self.before = ""
            self.after = ""
            self.delaybeforesend = 0.05

        def expect_exact(self, *_args, **_kwargs):
            return next(self.indices)

        def sendline(self, value):
            self.sent.append(value)

        def isalive(self):
            return True

        def eof(self):
            return False

        def close(self, **_kwargs):
            pass

    child = FakeChild()

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()

        @classmethod
        def spawn(cls, *_args, **_kwargs):
            return child

    cfg = replace(
        settings,
        hpc_auth_mode="password",
        hpc_password="session-secret",
        hpc_connect_retries=1,
    )
    client = HpcClient(cfg)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)

    with client._session_lock:
        client._connect_session_locked()

    assert child.sent[:3] == ["session-secret", "4", "2"]
    assert child.sent[-1] == "session-secret"
    assert child.sent.count("session-secret") == 2
    assert client.connection_diagnostic["stage"] == "ready"


def test_password_is_retained_after_post_auth_shell_error(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(
        client,
        "health",
        lambda: {
            "status": "unavailable",
            "message": "计算节点 Shell 初始化超时",
        },
    )

    result = client.authenticate_password("session-secret")

    assert result["status"] == "unavailable"
    assert client._password() == "session-secret"


def test_command_markers_cannot_be_satisfied_by_terminal_echo(monkeypatch) -> None:
    fixed_hex = "b" * 32
    pre_marker = "___PRE_bbbbbbbb___"
    marker = "___CMD_DONE_bbbbbbbb___"

    class FakeChild:
        def __init__(self):
            self.sent = []
            self.before = ""
            self.expect_count = 0

        def sendline(self, value):
            self.sent.append(value)

        def expect_exact(self, patterns, **_kwargs):
            self.expect_count += 1
            if self.expect_count == 1:
                assert patterns[0] == pre_marker
            elif self.expect_count == 2:
                assert patterns[:2] == ["$", "#"]
                assert "Select server:" in patterns
            else:
                assert patterns[0] == marker
                self.before = "payload\nEC:0\n"
            return 0

        def isalive(self):
            return True

        def eof(self):
            return False

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()

    client = HpcClient(settings)
    child = FakeChild()
    client._child = child
    monkeypatch.setattr(client, "_connect_session_locked", lambda: None)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)
    monkeypatch.setattr("hpc_transport.uuid.uuid4", lambda: SimpleNamespace(hex=fixed_hex))

    assert client._run_session_locked("printf payload", 30) == "payload"
    assert child.sent[0] == f"stty -echo; echo {pre_marker}"
    assert child.sent[1] == (
        f"( printf payload; ); __wrf_ec=$?; "
        f"printf 'EC:%s\\n' \"$__wrf_ec\"; echo {marker}"
    )
    assert child.sent[2] == "stty echo"


def test_remote_exit_is_isolated_from_persistent_login_shell(monkeypatch) -> None:
    class FakeChild:
        def __init__(self):
            self.indices = iter([0, 0, 0])
            self.sent = []
            self.before = "geogrid failed\nEC:21\n"
            self.after = ""
            self.closed = False

        def sendline(self, value):
            self.sent.append(value)

        def expect_exact(self, *_args, **_kwargs):
            return next(self.indices)

        def isalive(self):
            return not self.closed

        def eof(self):
            return self.closed

        def close(self, **_kwargs):
            self.closed = True

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()

    client = HpcClient(settings)
    child = FakeChild()
    client._child = child
    client._shell_ready = True
    monkeypatch.setattr(client, "_connect_session_locked", lambda: None)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)

    with pytest.raises(HpcError, match="geogrid failed"):
        client._run_session_locked("module load broken || exit 21", 30)

    assert child.sent[1].startswith("( module load broken || exit 21; )")
    assert child.closed is False
    assert client._child is child


def test_menu_session_is_closed_before_any_remote_command(monkeypatch) -> None:
    class FakeChild:
        def __init__(self):
            self.sent = []
            self.before = "4: log04 (172.18.1.178)\nSelect server: "
            self.after = "Select server:"
            self.closed = False

        def sendline(self, value):
            self.sent.append(value)

        def expect_exact(self, *_args, **_kwargs):
            return 1

        def isalive(self):
            return not self.closed

        def eof(self):
            return self.closed

        def close(self, **_kwargs):
            self.closed = True

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()

    client = HpcClient(settings)
    child = FakeChild()
    client._child = child
    client._shell_ready = True
    monkeypatch.setattr(client, "_connect_session_locked", lambda: None)
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)

    with pytest.raises(HpcSessionStaleError, match="离开计算节点 Shell"):
        client._run_session_locked("touch must-not-run", 30)

    assert len(child.sent) == 1
    assert "touch must-not-run" not in child.sent[0]
    assert child.closed is True
    assert client._child is None


def test_run_retries_once_only_when_command_was_not_dispatched(monkeypatch) -> None:
    client = HpcClient(settings)
    outcomes = iter([HpcSessionStaleError("stale"), "ready"])
    calls = []
    closes = []

    def execute(command, timeout):
        calls.append((command, timeout))
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(client, "_run_session_locked", execute)
    monkeypatch.setattr(client, "_close_session_locked", lambda: closes.append(True))

    assert client.run("printf ready", timeout=20) == "ready"
    assert calls == [("printf ready", 20), ("printf ready", 20)]
    assert closes == [True]


def test_remote_gfs_manifest_requires_size_and_sha_match(monkeypatch) -> None:
    cycle = "2026071500"
    digest0 = hashlib.sha256(b"zero0").hexdigest()
    digest6 = hashlib.sha256(b"six").hexdigest()
    manifest = {
        "product": GFS_PRODUCT,
        "scope": GFS_SCOPE,
        "cycle": cycle,
        "files": [
            {
                "name": "gfs.t00z.pgrb2.0p25.f000",
                "forecast_hour": 0,
                "size": 5,
                "sha256": digest0,
            },
            {
                "name": "gfs.t00z.pgrb2.0p25.f006",
                "forecast_hour": 6,
                "size": 3,
                "sha256": digest6,
            },
        ],
    }
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return json.dumps(manifest)
        return "gfs.t00z.pgrb2.0p25.f000|5\n"

    client = HpcClient(replace(settings, hpc_gfs_full_min_bytes=1))
    monkeypatch.setattr(client, "run", fake_run)

    result = client.inspect_gfs_files(cycle, [0, 6])

    assert result["valid_hours"] == [0]
    assert result["missing_hours"] == [6]
    assert result["complete"] is False


def test_remote_gfs_manifest_complete_skips_missing_list(monkeypatch) -> None:
    cycle = "2026071500"
    digest = "a" * 64
    manifest = {
        "product": GFS_PRODUCT,
        "scope": GFS_SCOPE,
        "cycle": cycle,
        "files": [
            {
                "name": "gfs.t00z.pgrb2.0p25.f000",
                "forecast_hour": 0,
                "size": 10,
                "sha256": digest,
            }
        ],
    }
    responses = iter([json.dumps(manifest), "gfs.t00z.pgrb2.0p25.f000|10"])
    client = HpcClient(replace(settings, hpc_gfs_full_min_bytes=1))
    monkeypatch.setattr(client, "run", lambda *_args, **_kwargs: next(responses))

    result = client.inspect_gfs_files(cycle, [0])

    assert result["complete"] is True
    assert result["missing_hours"] == []


def test_legacy_remote_gfs_without_full_manifest_is_not_reused(monkeypatch) -> None:
    cycle = "2026071500"
    calls = []
    client = HpcClient(replace(settings, hpc_import_legacy_gfs=True))

    def fake_run(command, **_kwargs):
        calls.append(command)
        return "{}"

    monkeypatch.setattr(client, "run", fake_run)

    result = client.inspect_gfs_files(cycle, [0, 6])

    assert result["complete"] is False
    assert result["valid_hours"] == []
    assert result["missing_hours"] == [0, 6]
    assert result["legacy_imported_hours"] == []
    assert result["manifest_needs_rebuild"] is False
    assert result["manifest_is_full"] is False
    assert len(calls) == 2


def test_wrf_script_checks_soil_layers_and_real_exit_status() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "wrf.sh").read_text(
        encoding="utf-8"
    )

    assert "num_st_layers" in script
    assert "num_sm_layers" in script
    assert 'WRF_NUM_METGRID_SOIL_LEVELS="$met_st_levels"' in script
    assert "real_status=${PIPESTATUS[0]}" in script
    assert "configure_runtime_stack" in script
    assert "ulimit -S -s unlimited" in script
    assert 'OMP_STACKSIZE="${OMP_STACKSIZE:-512M}"' in script
    assert 'KMP_STACKSIZE="${KMP_STACKSIZE:-512M}"' in script
    assert "ungrib_status=${PIPESTATUS[0]}" in script
    assert "ungrib.exe 栈内存异常" in script
    assert "io_form_gfdda" in script
    assert "wrffdda_d%02d" in script
    assert "*.pgrb2*" in script
    assert 'if [ "$shown_grib" -eq 0 ]' in script
    assert "ls -lh *.grb* *.grib* *.nc" not in script
    assert "find -L . -maxdepth 1 -type f -name 'met_em.d01.*' -print -quit" in script


def test_task_bundle_exports_complete_shell_runtime_without_remote_python(tmp_path) -> None:
    task_id = "wrf_gfs_20260720T120000Z_deadbeef"
    request = {
        "start_time": "2026-07-17T00:00:00Z",
        "end_time": "2026-07-17T06:00:00Z",
        "center": {"lat": 32.048, "lon": 118.825},
        "forecast_interval_hours": 6,
        "domains": [
            {
                "id": "d01", "dx": 27000, "dy": 27000,
                "e_we": 100, "e_sn": 79, "parent_id": 0,
                "parent_grid_ratio": 1, "i_parent_start": 1, "j_parent_start": 1,
            }
        ],
        "physics": {
            "preset": "默认通用", "mp_physics": 8, "cu_physics": 0,
            "ra_lw_physics": 4, "ra_sw_physics": 4, "bl_pbl_physics": 1,
            "sf_sfclay_physics": 1, "sf_surface_physics": 2,
            "sf_urban_physics": 0, "num_soil_layers": 4,
            "num_land_cat": 21, "radt": 5,
        },
        "assimilation_scheme": "fdda_standard",
    }
    entries = [
        {
            "name": f"gfs.t00z.pgrb2.0p25.f{hour:03d}",
            "forecast_hour": hour,
            "size": 100 + hour,
            "sha256": ("a" if hour == 0 else "b") * 64,
        }
        for hour in (0, 6)
    ]
    config_path = tmp_path / "task.json"
    environment_path = tmp_path / "task.env"
    expected_path = tmp_path / "gfs.expected.tsv"

    write_task_bundle(
        task_id, request, "2026071700", [0, 6], entries,
        config_path, environment_path, expected_path,
    )

    environment = environment_path.read_text(encoding="utf-8")
    assert "export WRF_DATA_SOURCE=gfs" in environment
    assert "export WRF_DX=27000" in environment
    assert "export WRF_DX_D01=27000" in environment
    assert "export WRF_ASSIM_GRID_FDDA=1" in environment
    assert "export WRF_ASSIM_GUV=0.0003" in environment
    assert "python" not in environment.lower()
    assert expected_path.read_text(encoding="utf-8").splitlines() == [
        f"gfs.t00z.pgrb2.0p25.f000\t100\t{'a' * 64}\t000",
        f"gfs.t00z.pgrb2.0p25.f006\t106\t{'b' * 64}\t006",
    ]


def test_hpc_gfs_entrypoint_has_no_python_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    entrypoint = (root / "scripts" / "wrf_hpc_gfs.sh").read_text(encoding="utf-8")
    assert "python3" not in entrypoint
    assert "source \"${WRF_TASK_ENV}\"" in entrypoint
    assert "preflight_hpc_runtime" in entrypoint


def test_launch_writes_pid_and_log_into_remote_task_directory(monkeypatch) -> None:
    task_id = "wrf_gfs_20260717T024751Z_4c119944"
    client = HpcClient(settings)
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return "50354"

    monkeypatch.setattr(client, "run", fake_run)

    result = client.launch(task_id)

    task_dir = client.task_dir(task_id)
    assert result["remote_pid"] == 50354
    assert "WRF_PREFLIGHT_ONLY=true" in commands[0]
    assert f"> {task_dir}/service.log" in commands[1]
    assert f"$$ > {task_dir}/service.pid" in commands[1]
    assert f"cat {task_dir}/service.pid" in commands[1]
    assert "WRF_TASK_ENV=" in commands[1]
    assert "WRF_GFS_EXPECTED_INDEX=" in commands[1]
    assert "pid=$!" not in commands[1]


def test_session_upload_uses_base64_chunks_and_remote_sha(monkeypatch) -> None:
    data = b"GRIB-test-payload"
    digest = hashlib.sha256(data).hexdigest()
    client = HpcClient(replace(settings, hpc_auth_mode="password", hpc_password="secret"))
    commands = []
    payloads = []
    metadata = iter([None, (0, hashlib.sha256(b"").hexdigest()), (len(data), digest)])

    class FakeChild:
        delaybeforesend = 0.05

        def expect_exact(self, *_args, **_kwargs):
            return 0

    client._child = FakeChild()
    monkeypatch.setattr(client, "_connect_session_locked", lambda: None)
    monkeypatch.setattr(client, "_session_alive_locked", lambda: True)
    monkeypatch.setattr(
        client,
        "_run_session_locked",
        lambda command, _timeout: commands.append(command) or "",
    )
    monkeypatch.setattr(client, "_remote_meta", lambda *_args, **_kwargs: next(metadata))
    monkeypatch.setattr(client, "_send_all_locked", payloads.append)

    client._upload_bytes_session_locked(
        BytesIO(data), len(data), digest, "~/Data/gfsdata/test.grib", 30
    )

    assert any(command.startswith("mkdir -p") for command in commands)
    assert any(command.startswith("mv ") for command in commands)
    assert len(payloads) == 1
    assert "base64 -d >>" in payloads[0]
    assert base64_payload_from_heredoc(payloads[0]) == data


def test_session_upload_uses_configured_small_chunks_and_timeout(monkeypatch) -> None:
    data = b"GRIB" + b"x" * (130 * 1024)
    digest = hashlib.sha256(data).hexdigest()
    cfg = replace(
        settings,
        hpc_auth_mode="password",
        hpc_password="secret",
        hpc_transfer_chunk_kb=64,
        hpc_transfer_chunk_timeout=45,
    )
    client = HpcClient(cfg)
    payloads = []
    timeouts = []
    metadata = iter([None, (0, hashlib.sha256(b"").hexdigest()), (len(data), digest)])

    class FakeChild:
        delaybeforesend = 0.05

        def expect_exact(self, *_args, **kwargs):
            timeouts.append(kwargs["timeout"])
            return 0

    client._child = FakeChild()
    monkeypatch.setattr(client, "_connect_session_locked", lambda: None)
    monkeypatch.setattr(client, "_session_alive_locked", lambda: True)
    monkeypatch.setattr(client, "_run_session_locked", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(client, "_remote_meta", lambda *_args, **_kwargs: next(metadata))
    monkeypatch.setattr(client, "_send_all_locked", payloads.append)

    client._upload_bytes_session_locked(
        BytesIO(data), len(data), digest, "~/Data/gfsdata/test.grib", 1800
    )

    assert len(payloads) == 3
    assert timeouts == [45, 45, 45]


def test_pty_upload_reconnects_and_resumes_after_chunk_failure(monkeypatch) -> None:
    source = Path(__file__).resolve().parents[1] / "scripts" / "wrf_hpc_gfs.sh"
    cfg = replace(
        settings,
        hpc_transfer_mode="pty",
        hpc_transfer_retries=2,
    )
    client = HpcClient(cfg)
    calls = []
    progress = []

    def flaky_upload(_source, size, _digest, _remote, _timeout, callback):
        calls.append(size)
        if len(calls) == 1:
            raise HpcError("模拟单块确认超时")
        callback(size, size)

    monkeypatch.setattr(client, "_upload_bytes_session_locked", flaky_upload)
    monkeypatch.setattr("hpc_transport.time.sleep", lambda _seconds: None)

    client.upload(
        source,
        "~/Data/gfsdata/wrf_hpc_gfs.sh",
        progress=lambda done, total: progress.append((done, total)),
    )

    assert len(calls) == 2
    assert progress[-1][0] == progress[-1][1]
    assert client.transfer_status["mode"] == "pty_resumed"


def test_auto_sftp_failure_falls_back_to_resumable_pty(monkeypatch) -> None:
    root = Path("/tmp/zhihuiqixiang-wrf-parallel-tests-20260716") / uuid.uuid4().hex
    root.mkdir(parents=True)
    local_path = root / "gfs.test"
    local_path.write_bytes(b"GRIB-test")
    client = HpcClient(replace(settings, hpc_transfer_mode="auto"))
    calls = []
    progress = []
    progress_states = []

    def fail_sftp(*_args, **_kwargs):
        raise HpcError("模拟 SFTP 不可用")

    def fake_pty(source, size, _digest, remote_path, _timeout, callback):
        calls.append((source.read(), remote_path))
        callback(size, size)

    monkeypatch.setattr(client, "_upload_sftp", fail_sftp)
    monkeypatch.setattr(client, "_upload_bytes_session_locked", fake_pty)

    client.upload(
        local_path,
        "~/Data/gfsdata/2026071600/gfs.test",
        progress=lambda done, total: (
            progress.append((done, total)),
            progress_states.append(dict(client.transfer_status)),
        ),
    )

    assert calls == [(b"GRIB-test", "~/Data/gfsdata/2026071600/gfs.test")]
    assert progress == [(len(b"GRIB-test"), len(b"GRIB-test"))]
    assert progress_states[0]["state"] == "running"
    assert progress_states[0]["mode"] == "pty_fallback"
    assert client.transfer_status["mode"] == "pty_fallback"
    assert client.transfer_status["state"] == "succeeded"
    assert client.transfer_status["message"] == "原生 SFTP 不可用，PTY 回退传输成功"


def test_strict_sftp_failure_reports_failed_state(monkeypatch) -> None:
    client = HpcClient(replace(settings, hpc_transfer_mode="sftp"))
    monkeypatch.setattr(
        client,
        "_upload_sftp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HpcError("模拟 SFTP 失败")),
    )

    with pytest.raises(HpcError, match="模拟 SFTP 失败"):
        client.upload(Path("/tmp/not-read-after-sftp-failure"), "~/remote.test")

    assert client.transfer_status == {
        "mode": "sftp",
        "state": "failed",
        "message": "模拟 SFTP 失败",
    }


def test_direct_scp_failure_reports_failed_state(monkeypatch) -> None:
    client = HpcClient(replace(settings, hpc_connection_mode="direct"))
    monkeypatch.setattr(
        client,
        "_run_direct",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HpcError("模拟 SCP 失败")),
    )

    with pytest.raises(HpcError, match="模拟 SCP 失败"):
        client.upload(Path("/tmp/not-read-after-scp-failure"), "~/remote.test")

    assert client.transfer_status == {
        "mode": "scp",
        "state": "failed",
        "message": "模拟 SCP 失败",
    }


def test_bastion_sftp_navigates_menus_and_reports_progress(monkeypatch) -> None:
    root = Path("/tmp/zhihuiqixiang-wrf-parallel-tests-20260716") / uuid.uuid4().hex
    root.mkdir(parents=True)
    local_path = root / "gfs.test"
    data = b"GRIB-sftp-test"
    local_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    class FakeMatch:
        def group(self, _index=0):
            return "50"

    class FakeChild:
        def __init__(self):
            self.indices = iter([0, 1, 2, 4, 1, 0, 7])
            self.sent = []
            self.match = FakeMatch()
            self.delaybeforesend = 0.05

        def expect(self, *_args, **_kwargs):
            return next(self.indices)

        def sendline(self, value):
            self.sent.append(value)

        def isalive(self):
            return True

        def close(self, **_kwargs):
            pass

    child = FakeChild()

    class FakePexpect:
        EOF = object()
        TIMEOUT = object()

        @staticmethod
        def spawn(*_args, **_kwargs):
            return child

    cfg = replace(
        settings,
        hpc_auth_mode="password",
        hpc_password="session-secret",
        hpc_transfer_mode="sftp",
    )
    client = HpcClient(cfg)
    metadata = iter([None, None, (len(data), digest)])
    commands = []
    progress = []
    monkeypatch.setattr(client, "_pexpect", lambda: FakePexpect)
    monkeypatch.setattr(client, "_absolute_remote_path", lambda _path: "/remote/gfs.test")
    monkeypatch.setattr(client, "_remote_meta", lambda *_args, **_kwargs: next(metadata))
    monkeypatch.setattr(client, "run", lambda command, **_kwargs: commands.append(command) or "")

    client._upload_sftp(
        local_path,
        "~/Data/gfsdata/gfs.test",
        30,
        lambda done, total: progress.append((done, total)),
    )

    assert child.sent[:3] == ["session-secret", "4", "2"]
    assert child.sent[3].startswith("reput ")
    assert child.sent[-1] == "bye"
    assert progress[-1] == (len(data), len(data))
    assert any(command.startswith("mv ") for command in commands)


def test_upload_does_not_swallow_cancellation_from_progress(monkeypatch) -> None:
    class CancelSignal(RuntimeError):
        pass

    client = HpcClient(replace(settings, hpc_transfer_mode="auto"))

    def cancelled(_local, _remote, _timeout, progress):
        progress(1, 2)

    monkeypatch.setattr(client, "_upload_sftp", cancelled)
    monkeypatch.setattr(
        client,
        "_upload_bytes_session_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("取消信号不应触发 PTY 回退")
        ),
    )

    with pytest.raises(CancelSignal):
        client.upload(
            Path("/tmp/zhihuiqixiang-cancel-signal-no-file"),
            "~/Data/gfsdata/test",
            progress=lambda *_args: (_ for _ in ()).throw(CancelSignal()),
        )


def test_session_download_reconnects_after_invalid_base64(monkeypatch, tmp_path) -> None:
    data = b"WRF-output-test"
    client = HpcClient(replace(settings, hpc_transfer_retries=1))
    responses = iter(["%%%invalid-base64%%%", base64.b64encode(data).decode("ascii")])
    reconnects = []
    target = tmp_path / "wrfout_d01_test"

    monkeypatch.setattr(
        client,
        "_remote_meta",
        lambda *_args, **_kwargs: (len(data), hashlib.sha256(data).hexdigest()),
    )
    monkeypatch.setattr(client, "run", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(client, "close_session", lambda: reconnects.append(True))
    monkeypatch.setattr("hpc_transport.time.sleep", lambda _seconds: None)

    client._download_file_session("~/WRF/run/wrfout_d01_test", target, 30)

    assert target.read_bytes() == data
    assert not target.with_name(target.name + ".part").exists()
    assert reconnects == [True]


def test_session_download_resumes_from_aligned_part(monkeypatch, tmp_path) -> None:
    prefix = b"A" * (1024 * 1024)
    suffix = b"WRF-tail"
    data = prefix + suffix
    client = HpcClient(
        replace(settings, hpc_transfer_retries=1, hpc_download_chunk_mb=1)
    )
    target = tmp_path / "wrfout_d02_test"
    part = target.with_name(target.name + ".part")
    part.write_bytes(prefix)
    commands = []

    monkeypatch.setattr(
        client,
        "_remote_meta",
        lambda *_args, **_kwargs: (len(data), hashlib.sha256(data).hexdigest()),
    )

    def run(command, **_kwargs):
        commands.append(command)
        return base64.b64encode(suffix).decode("ascii")

    monkeypatch.setattr(client, "run", run)

    client._download_file_session("~/WRF/run/wrfout_d02_test", target, 30)

    assert target.read_bytes() == data
    assert "skip=1" in commands[0]


def test_session_download_batches_remote_inventory(monkeypatch, tmp_path) -> None:
    first = b"first"
    second = b"second-output"
    inventory = "\n".join(
        [
            f"FILE:wrfout_d01_a SIZE:{len(first)} SHA:{hashlib.sha256(first).hexdigest()}",
            f"FILE:wrfout_d01_b SIZE:{len(second)} SHA:{hashlib.sha256(second).hexdigest()}",
        ]
    )
    client = HpcClient(settings)
    remote_queries = []
    downloads = []
    progress = []

    monkeypatch.setattr(
        client,
        "run",
        lambda command, **_kwargs: remote_queries.append(command) or inventory,
    )

    def download(remote_path, local_path, _timeout, *, expected_meta, progress):
        downloads.append((remote_path, local_path.name, expected_meta))
        progress(expected_meta[0], expected_meta[0])

    monkeypatch.setattr(client, "_download_file_session", download)

    client.download_glob(
        "~/WRF/run/wrfout_d*_*",
        tmp_path,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert len(remote_queries) == 1
    assert "sha256sum" in remote_queries[0]
    assert [item[1] for item in downloads] == ["wrfout_d01_a", "wrfout_d01_b"]
    assert downloads[0][2] == (len(first), hashlib.sha256(first).hexdigest())
    assert downloads[1][2] == (len(second), hashlib.sha256(second).hexdigest())
    assert progress[0] == (0, len(first) + len(second))
    assert progress[-1] == (len(first) + len(second), len(first) + len(second))


def base64_payload_from_heredoc(payload: str) -> bytes:
    lines = payload.splitlines()
    delimiter = lines[0].rsplit("<<'", 1)[1].rstrip("'")
    end = lines.index(delimiter)
    import base64

    return base64.b64decode("".join(lines[1:end]))
