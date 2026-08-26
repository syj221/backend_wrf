from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import uuid
import pytest

from config import settings
from hpc import HpcAuthError, HpcClient, HpcError, write_task_bundle
from hpc_transport import GFS_PRODUCT, GFS_SCOPE, HpcSessionStaleError


def test_default_wrf_source_directories_match_tx_lab_layout() -> None:
    assert settings.hpc_wrf_source_dir == "/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU"
    assert settings.hpc_wrf_gpu_source_dir == "/home/tx-lab/WRFwork/WRF_BUILD/WRF_GPU"
    script_dir = Path(__file__).parents[1] / "scripts"
    launch_script = (script_dir / "wrf_hpc_gfs.sh").read_text()
    runtime_script = (script_dir / "wrf.sh").read_text()
    assert "/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU" in launch_script
    assert "/home/tx-lab/WRFwork/WRF_BUILD/WRF_GPU" not in launch_script
    assert 'WRF_REQUESTED_RUNTIME_PROFILE="cpu"' in launch_script
    assert '${WRF_SOURCE_DIR}/main/real.exe' in runtime_script
    assert '${WRF_SOURCE_DIR}/main/wrf.exe' in runtime_script


def test_hpc_health_requires_runtime_directories(monkeypatch) -> None:
    client = HpcClient(replace(settings, hpc_remote_dir="~/WRFwork", hpc_gfs_dir="~/Data/gfsdata"))
    commands: list[str] = []

    def unavailable(command: str, **_kwargs) -> str:
        commands.append(command)
        return "缺少: WPS_GEOG"

    monkeypatch.setattr(client, "run", unavailable)
    assert client.health()["status"] == "unavailable"
    assert f"{settings.hpc_wrf_source_dir}/main/real.exe" in commands[-1]
    assert f"{settings.hpc_wrf_source_dir}/main/wrf.exe" in commands[-1]
    assert settings.hpc_wrf_gpu_source_dir not in commands[-1]
    monkeypatch.setattr(client, "run", lambda *_args, **_kwargs: "WRF_HPC_READY")
    assert client.health()["status"] == "ready"


def test_direct_transport_uses_port_key_and_strict_known_hosts() -> None:
    cfg = replace(
        settings,
        hpc_connection_mode="direct",
        hpc_port=1301,
        hpc_key_file="/tmp/wrf-key",
        hpc_known_hosts_file="/tmp/wrf-known-hosts",
    )
    client = HpcClient(cfg)

    ssh = client._ssh_base()
    scp = client._scp_base()
    sftp = client._sftp_base()

    assert ssh[ssh.index("-p") + 1] == "1301"
    assert scp[scp.index("-P") + 1] == "1301"
    assert sftp[sftp.index("-P") + 1] == "1301"
    assert "StrictHostKeyChecking=yes" in ssh
    assert "UserKnownHostsFile=/tmp/wrf-known-hosts" in ssh
    assert ssh[ssh.index("-i") + 1] == "/tmp/wrf-key"


def test_task_artifact_paths_are_exact_and_exclude_shared_gfs(monkeypatch) -> None:
    client = HpcClient(settings)
    task_id = "wrf_gfs_20260722T000000Z_deadbeef"
    monkeypatch.setattr(client, "_absolute_remote_path", lambda _path: "/share/home/user/WRFwork")

    paths = client.task_artifact_paths(task_id)

    assert paths[0] == f"/share/home/user/WRFwork/backend_wrf_tasks/{task_id}"
    assert f"/share/home/user/WRFwork/WRF_{task_id}" in paths
    assert f"/share/home/user/WRFwork/WRF_{task_id}_attempt-1-gpu" in paths
    assert f"/share/home/user/WRFwork/WRF_{task_id}_attempt-2-cpu" in paths
    assert all("gfsdata" not in path for path in paths)


def test_cleanup_task_attempt_refuses_running_remote_process(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(client, "status", lambda _task_id: {"status": "running"})

    with pytest.raises(HpcError, match="仍在运行"):
        client.cleanup_task_attempt("wrf_gfs_20260722T000000Z_deadbeef")


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
    assert item["auto_cleanup_candidates"] == ["/share/home/user/Data/gfsdata/2026071900"]
    assert cycles["2026071800"]["cleanup_allowed"] is False
    assert cycles["2026071700"]["protected"] is True


def test_gfs_pool_allows_old_cycle_cleanup_while_target_is_incomplete(monkeypatch) -> None:
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
    cycles = {cycle["cycle"]: cycle for cycle in item["cycles"]}

    assert item["targets_complete"] is False
    assert item["cleanup_candidates"] == ["/share/home/user/Data/gfsdata/2026071900"]
    assert item["auto_cleanup_candidates"] == ["/share/home/user/Data/gfsdata/2026071900"]
    assert cycles["2026072100"]["cleanup_allowed"] is False
    assert cycles["2026072000"]["cleanup_allowed"] is False


def test_gfs_pool_never_auto_cleans_cycle_newer_than_historical_target(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(
        client,
        "run",
        lambda *_args, **_kwargs: "\n".join(
            [
                "ROOT|/share/home/user/Data/gfsdata",
                "2026072200|ready|73|1000|072",
                "2026072100|missing|0|0|0",
                "2026072000|ready|73|900|072",
            ]
        ),
    )

    item = client.gfs_pool_items(["2026072100"], set())[0]
    cycles = {cycle["cycle"]: cycle for cycle in item["cycles"]}

    assert item["cleanup_candidates"] == [
        "/share/home/user/Data/gfsdata/2026072200",
        "/share/home/user/Data/gfsdata/2026072000",
    ]
    assert item["auto_cleanup_candidates"] == ["/share/home/user/Data/gfsdata/2026072000"]
    assert cycles["2026072200"]["auto_cleanup_allowed"] is False
    assert cycles["2026072000"]["auto_cleanup_allowed"] is True


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


def test_gfs_pool_reports_partial_bytes_and_active_downloads(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(
        client,
        "run",
        lambda *_args, **_kwargs: "\n".join(
            [
                "ROOT|/share/home/user/Data/gfsdata",
                "2026072100|downloading|3|1525922594|002|4|209715200|4|DOWNLOAD f003 mirror=aws",
            ]
        ),
    )

    cycle = client.gfs_pool_items(["2026072100"], set())[0]["cycles"][0]

    assert cycle["partial_files"] == 4
    assert cycle["partial_size_bytes"] == 209715200
    assert cycle["active_downloads"] == 4
    assert cycle["download_message"] == "DOWNLOAD f003 mirror=aws"


def test_validate_outputs_isolates_ncdump_without_remote_temp_file(monkeypatch) -> None:
    client = HpcClient(settings)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return "VALID|wrfout_d01_2026-07-28_00:00:00|9482145|ok"

    monkeypatch.setattr(client, "run", run)

    result = client.validate_outputs("wrf_gfs_20260729T073138Z_4e412a30")

    assert result == {
        "valid": [{
            "name": "wrfout_d01_2026-07-28_00:00:00",
            "size": 9482145,
            "reason": "ok",
        }],
        "invalid": [],
        "complete": True,
    }
    assert len(commands) == 1
    assert settings.hpc_runtime_env in commands[0]
    assert 'LD_LIBRARY_PATH= "$ncdump_path" -h "$name"' in commands[0]
    assert ".wrfout_sizes_1" not in commands[0]


def test_trigger_gfs_download_reports_immediate_remote_failure(monkeypatch) -> None:
    client = HpcClient(settings)
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return "FAILED|curl: (28) NOAA request timed out"

    monkeypatch.setattr(client, "run", run)
    monkeypatch.setattr(client, "upload", lambda *_args, **_kwargs: None)

    result = client.trigger_gfs_download("2026072100", 72, forecast_hours=[12, 15, 18])

    assert result == {
        "cycle": "2026072100",
        "status": "failed",
        "detail": "curl: (28) NOAA request timed out",
    }
    assert any("kill -0" in command for command in commands)
    assert any("flock -n" in command for command in commands)
    assert all("pgrep -f" not in command for command in commands)
    assert any("download_2026072100.out" in command for command in commands)
    assert any("download_gfs_00z.sh 2026072100 72 12,15,18" in command for command in commands)
    assert any(f"WRF_GFS_MIN_SPEED_BPS={settings.hpc_gfs_min_speed_bps}" in command for command in commands)
    assert any(f"WRF_GFS_SLOW_SECONDS={settings.hpc_gfs_slow_seconds}" in command for command in commands)


def test_trigger_gfs_download_does_not_replace_script_while_lock_is_busy(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(client, "run", lambda *_args, **_kwargs: "RUNNING")

    def unexpected_upload(*_args, **_kwargs):
        raise AssertionError("运行中的共享下载不得覆盖远端脚本")

    monkeypatch.setattr(client, "upload", unexpected_upload)

    assert client.trigger_gfs_download("2026072100", 72) == {
        "cycle": "2026072100",
        "status": "running",
        "detail": "shared",
    }


def test_download_script_fetches_regional_files_serially_and_keeps_other_cycles(tmp_path) -> None:
    fake_bin = tmp_path / "bin"
    mount_root = tmp_path / "mount"
    data_root = mount_root / "WRF" / "GFS"
    fake_bin.mkdir()
    data_root.mkdir(parents=True)
    retained_cycle = data_root / "2026072700"
    retained_cycle.mkdir()
    (retained_cycle / "keep.txt").write_text("keep", encoding="utf-8")

    commands = {
        "findmnt": "#!/usr/bin/env bash\nexit 0\n",
        "flock": "#!/usr/bin/env bash\nexit 0\n",
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
        "df": (
            "#!/usr/bin/env bash\n"
            "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
            "printf 'fake 200000000 1 199999999 1%% %s\\n' \"${@: -1}\"\n"
        ),
        "curl": r'''#!/usr/bin/env bash
output=""
url=""
while (($#)); do
    case "$1" in
        --output) output="$2"; shift 2 ;;
        --connect-timeout|--max-time|--speed-limit|--speed-time|--continue-at)
            shift 2 ;;
        --fail|--location|--silent|--show-error) shift ;;
        http*) url="$1"; shift ;;
        *) shift ;;
    esac
done
printf '%s\n' "$url" >> "$WRF_TEST_URL_LOG"
mkdir -p "$(dirname "$output")"
truncate -s 1048576 "$output"
printf GRIB | dd of="$output" bs=1 seek=0 conv=notrunc status=none
printf 7777 | dd of="$output" bs=1 seek=1048572 conv=notrunc status=none
''',
    }
    for name, content in commands.items():
        path = fake_bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    script = Path(__file__).resolve().parents[1] / "scripts" / "download_gfs_00z.sh"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WRF_GFS_DATA_ROOT": str(data_root),
        "WRF_GFS_MOUNT": str(mount_root),
        "WRF_GFS_MIN_FREE_GB": "100",
        "WRF_GFS_MIN_BYTES": "65536",
        "WRF_GFS_MIN_SPEED_BPS": "1024",
        "WRF_GFS_SLOW_SECONDS": "30",
        "WRF_GFS_REQUEST_INTERVAL_SECONDS": "10",
        "WRF_TEST_URL_LOG": str(tmp_path / "urls.log"),
    }

    result = subprocess.run(
        ["bash", str(script), "2026072800", "72", "0,2,3,4,5"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "REGIONAL_DOWNLOAD 2026072800 bounds=65,5,145,60 interval=10s" in result.stdout
    assert "DOWNLOAD_COMPLETE 2026072800 5" in result.stdout
    outputs = sorted((data_root / "2026072800").glob("gfs.t00z.pgrb2.0p25.f???"))
    assert len(outputs) == 5
    assert not (data_root / "2026072800" / "gfs.t00z.pgrb2.0p25.f001").exists()
    assert all(path.stat().st_size == 1048576 for path in outputs)
    assert (retained_cycle / "keep.txt").read_text(encoding="utf-8") == "keep"
    urls = (tmp_path / "urls.log").read_text(encoding="utf-8").splitlines()
    assert len(urls) == 5
    assert all("filter_gfs_0p25.pl" in url for url in urls)
    assert all("all_lev=on&all_var=on" in url for url in urls)
    assert all("leftlon=65&rightlon=145&toplat=60&bottomlat=5" in url for url in urls)


def test_ensure_remote_gfs_stops_on_immediate_download_failure(monkeypatch) -> None:
    client = HpcClient(settings)
    monkeypatch.setattr(
        client,
        "inspect_gfs_files",
        lambda *_args, **_kwargs: {"complete": False, "valid_hours": [], "missing_hours": [0]},
    )
    monkeypatch.setattr(
        client,
        "trigger_gfs_download",
        lambda *_args, **_kwargs: {"status": "failed", "detail": "404 Not Found"},
    )
    monkeypatch.setattr(
        client,
        "auto_cleanup_gfs_cycles",
        lambda *_args, **_kwargs: {"requested": [], "deleted": [], "missing": []},
    )

    with pytest.raises(HpcError, match="404 Not Found"):
        client.ensure_remote_gfs("2026072100", [0])


def test_ensure_remote_gfs_auto_cleans_before_trigger(monkeypatch) -> None:
    client = HpcClient(settings)
    events = []
    monkeypatch.setattr(
        client,
        "inspect_gfs_files",
        lambda *_args, **_kwargs: {"complete": False, "valid_hours": [], "missing_hours": [0]},
    )

    def cleanup(target_cycles, protected_cycles):
        events.append(("cleanup", target_cycles, protected_cycles))
        return {"requested": ["/gfs/2026072000"], "deleted": ["/gfs/2026072000"], "missing": []}

    def trigger(cycle, horizon, forecast_hours=None):
        events.append(("trigger", cycle, horizon, forecast_hours))
        return {"status": "failed", "detail": "download unavailable"}

    monkeypatch.setattr(client, "auto_cleanup_gfs_cycles", cleanup)
    monkeypatch.setattr(client, "trigger_gfs_download", trigger)

    with pytest.raises(HpcError, match="download unavailable"):
        client.ensure_remote_gfs(
            "2026072100",
            [0],
            protected_cycles={"2026072000"},
        )

    assert events == [
        ("cleanup", ["2026072100"], {"2026072000"}),
        ("trigger", "2026072100", 72, [0]),
    ]


def test_ensure_remote_gfs_retries_after_shared_lock_is_released(monkeypatch) -> None:
    client = HpcClient(replace(settings, hpc_gfs_poll_seconds=0, hpc_gfs_wait_seconds=60))
    snapshots = iter([
        {"complete": False, "valid_hours": [], "missing_hours": [12, 15]},
        {"complete": False, "valid_hours": [], "missing_hours": [12, 15]},
        {"complete": True, "valid_hours": [12, 15], "missing_hours": []},
    ])
    triggers = []
    monkeypatch.setattr(client, "inspect_gfs_files", lambda *_args, **_kwargs: next(snapshots))
    monkeypatch.setattr(
        client,
        "auto_cleanup_gfs_cycles",
        lambda *_args, **_kwargs: {"requested": [], "deleted": [], "missing": []},
    )

    def trigger(cycle, horizon, forecast_hours=None):
        triggers.append((cycle, horizon, forecast_hours))
        state = "running" if len(triggers) == 1 else "started"
        return {"cycle": cycle, "status": state, "detail": "shared" if state == "running" else "123"}

    monkeypatch.setattr(client, "trigger_gfs_download", trigger)

    result = client.ensure_remote_gfs("2026072100", [12, 15])

    assert result["complete"] is True
    assert triggers == [
        ("2026072100", 72, [12, 15]),
        ("2026072100", 72, [12, 15]),
    ]


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


def test_auto_cleanup_gfs_cycles_uses_only_auto_allowlist(monkeypatch) -> None:
    client = HpcClient(settings)
    allowed = "/share/home/user/Data/gfsdata/2026071900"
    manual_only = "/share/home/user/Data/gfsdata/2026072200"
    monkeypatch.setattr(
        client,
        "gfs_pool_items",
        lambda *_args, **_kwargs: [{
            "cleanup_candidates": [allowed, manual_only],
            "auto_cleanup_candidates": [allowed],
        }],
    )
    cleaned = []

    def cleanup(paths, target_cycles, protected_cycles):
        cleaned.extend(paths)
        assert target_cycles == ["2026072100"]
        assert protected_cycles == {"2026071800"}
        return {"deleted": list(paths), "missing": []}

    monkeypatch.setattr(client, "cleanup_gfs_cycles", cleanup)

    result = client.auto_cleanup_gfs_cycles(["2026072100"], {"2026071800"})

    assert cleaned == [allowed]
    assert result == {"requested": [allowed], "deleted": [allowed], "missing": []}


def test_hpc_permission_error_requires_interactive_auth(monkeypatch) -> None:
    client = HpcClient(settings)

    def deny(*_args, **_kwargs):
        raise HpcAuthError("超算密码认证失败")

    monkeypatch.setattr(client, "run", deny)
    result = client.health()
    assert result["status"] == "auth_required"
    assert result["message"] == "tx-lab SSH 密钥未就绪"
    assert result["connection_mode"] == "direct"


def test_hpc_password_is_kept_only_for_ready_process_session(monkeypatch) -> None:
    client = HpcClient(replace(settings, hpc_connection_mode="bastion"))
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

    cfg = replace(
        settings,
        hpc_host="chaosuan",
        hpc_connection_mode="bastion",
        hpc_auth_mode="password",
        hpc_password="session-secret",
    )
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
    client = HpcClient(replace(settings, hpc_connection_mode="bastion"))
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

    client = HpcClient(replace(settings, hpc_connection_mode="bastion", hpc_auth_mode="password"))
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
    client = HpcClient(replace(settings, hpc_connection_mode="bastion", hpc_auth_mode="password"))
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
        "bounds": [65.0, 5.0, 145.0, 60.0],
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
        "bounds": [65.0, 5.0, 145.0, 60.0],
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
    assert 'mpirun -np "$mpi_processes" ./real.exe 2>&1 | tee real.log' in script
    assert "if ./real.exe 2>&1 | tee real.log" not in script
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
    assert "adjust_output_times                 = .true." in script
    assert "输出时次检查" in script


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
        "runtime_profile": "gpu",
        "forecast_focus": "convection",
        "spinup": {"mode": "custom", "hours": 24},
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
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["gfs_cache"]["gfs_file_interval_hours"] == 6
    assert config["runtime_profile"] == "cpu"
    assert config["time_range"]["spinup_hours"] == 6
    assert config["time_range"]["model_start"] == "2026-07-16T18:00:00+00:00"
    assert config["assimilation"]["end_hour"] == 6
    assert "export WRF_REQUESTED_RUNTIME_PROFILE=cpu" in environment
    assert "export WRF_ASSIM_SPINUP_HOURS=6" in environment
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
    assert "WRF_GPU_SOURCE_DIR=" not in commands[1]
    assert "WRF_GPU_MPI_PROCESSES=" not in commands[1]
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
        hpc_connection_mode="bastion",
        hpc_auth_mode="password",
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
    client = HpcClient(replace(settings, hpc_connection_mode="bastion", hpc_auth_mode="password", hpc_transfer_mode="auto"))
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
    client = HpcClient(replace(settings, hpc_connection_mode="bastion", hpc_auth_mode="password", hpc_transfer_mode="sftp"))
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

    client = HpcClient(replace(settings, hpc_connection_mode="bastion", hpc_auth_mode="password", hpc_transfer_mode="auto"))

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
    client = HpcClient(replace(settings, hpc_connection_mode="bastion", hpc_auth_mode="password"))
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
