from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta

from config import Settings


SAFE_TASK_ID = re.compile(r"^wrf_(?:gfs_)?[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
SAFE_REMOTE_ROOT = re.compile(r"^(?:~|/)[A-Za-z0-9_./-]*$")
SAFE_GFS_NAME = re.compile(r"^gfs\.t(?P<cycle_hour>[0-9]{2})z\.pgrb2\.0p25\.f(?P<forecast_hour>[0-9]{3})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HpcError(RuntimeError):
    pass


class _LegacyHpcClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._credential_lock = threading.RLock()
        self._session_password: str | None = None
        if not SAFE_REMOTE_ROOT.fullmatch(settings.hpc_remote_dir):
            raise ValueError("WRF_HPC_REMOTE_DIR 包含不安全字符")
        if not SAFE_REMOTE_ROOT.fullmatch(settings.hpc_gfs_dir):
            raise ValueError("WRF_HPC_GFS_DIR 包含不安全字符")
        for label, value in (
            ("WRF_HPC_WPS_SOURCE_DIR", settings.hpc_wps_source_dir),
            ("WRF_HPC_WRF_SOURCE_DIR", settings.hpc_wrf_source_dir),
            ("WRF_HPC_GEOG_DATA_PATH", settings.hpc_geog_dir),
        ):
            if not SAFE_REMOTE_ROOT.fullmatch(value):
                raise ValueError(f"{label} 包含不安全字符")

    @property
    def target(self) -> str:
        return f"{self.settings.hpc_user}@{self.settings.hpc_host}" if self.settings.hpc_user else self.settings.hpc_host

    def _uses_password(self) -> bool:
        with self._credential_lock:
            return self._session_password is not None or self.settings.hpc_auth_mode == "password"

    def _password(self) -> str:
        with self._credential_lock:
            return self._session_password if self._session_password is not None else self.settings.hpc_password

    def _ssh_base(self) -> list[str]:
        command = [
            "ssh", "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        ]
        if self._uses_password():
            command.extend([
                "-tt", "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no",
            ])
        elif self.settings.hpc_auth_mode == "key":
            command.extend(["-o", "BatchMode=yes"])
        if self.settings.hpc_key_file:
            command.extend(["-i", self.settings.hpc_key_file])
        command.append(self.target)
        return command

    def _scp_base(self) -> list[str]:
        command = ["scp", "-q", "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}"]
        if self._uses_password():
            command.extend([
                "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no",
            ])
        elif self.settings.hpc_auth_mode == "key":
            command.extend(["-o", "BatchMode=yes"])
        if self.settings.hpc_key_file:
            command.extend(["-i", self.settings.hpc_key_file])
        return command

    def _run_with_system_expect(
        self, command: list[str], password: str, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        expect_bin = shutil.which("expect")
        if not expect_bin:
            raise HpcError("密码认证需要 Python pexpect 或系统 expect")
        environment = os.environ.copy()
        environment.update(
            {
                "WRF_EXPECT_PASSWORD": password,
                "WRF_EXPECT_SERVER_INDEX": os.getenv("WRF_HPC_SERVER_INDEX", "4"),
                "WRF_EXPECT_ACCOUNT_INDEX": os.getenv("WRF_HPC_ACCOUNT_INDEX", "2"),
                "WRF_EXPECT_TIMEOUT": str(max(1, int(timeout))),
            }
        )
        script = r'''
set timeout $env(WRF_EXPECT_TIMEOUT)
set password $env(WRF_EXPECT_PASSWORD)
set server_index $env(WRF_EXPECT_SERVER_INDEX)
set account_index $env(WRF_EXPECT_ACCOUNT_INDEX)
spawn -noecho {*}$argv
expect {
    -nocase -re {password:} { send -- "$password\r"; exp_continue }
    -exact "Select server:" { send -- "$server_index\r"; exp_continue }
    -exact "Select account:" { send -- "$account_index\r"; exp_continue }
    eof {
        set wait_result [wait]
        exit [lindex $wait_result 3]
    }
    timeout {
        puts stderr "超算命令执行超时"
        exit 124
    }
}
'''
        try:
            result = subprocess.run(
                [expect_bin, "-f", "-", "--", *command],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise HpcError("超算命令执行超时") from exc
        if result.returncode:
            raise HpcError((result.stderr or result.stdout or "超算命令失败").strip())
        return result

    def _run_ssh_with_system_expect(
        self,
        ssh_command: list[str],
        remote_command: str,
        password: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        expect_bin = shutil.which("expect")
        if not expect_bin:
            raise HpcError("交互式超算认证需要 Python pexpect 或系统 expect")
        environment = os.environ.copy()
        environment.update(
            {
                "WRF_EXPECT_PASSWORD": password,
                "WRF_EXPECT_SERVER_INDEX": os.getenv("WRF_HPC_SERVER_INDEX", "4"),
                "WRF_EXPECT_ACCOUNT_INDEX": os.getenv("WRF_HPC_ACCOUNT_INDEX", "2"),
                "WRF_EXPECT_REMOTE_COMMAND": remote_command,
                "WRF_EXPECT_TIMEOUT": str(max(1, int(timeout))),
            }
        )
        script = r'''
set timeout $env(WRF_EXPECT_TIMEOUT)
set password $env(WRF_EXPECT_PASSWORD)
set server_index $env(WRF_EXPECT_SERVER_INDEX)
set account_index $env(WRF_EXPECT_ACCOUNT_INDEX)
set remote_command $env(WRF_EXPECT_REMOTE_COMMAND)
set transcript ""
set terminal_prepared 0
set command_sent 0
log_user 0
spawn -noecho {*}$argv
while {1} {
    expect {
        -nocase -re {password:} {
            append transcript $expect_out(buffer)
            send -- "$password\r"
        }
        -exact "Select server:" {
            append transcript $expect_out(buffer)
            send -- "$server_index\r"
        }
        -exact "Select account:" {
            append transcript $expect_out(buffer)
            send -- "$account_index\r"
        }
        -re {(?m)[$#] ?$} {
            if {!$terminal_prepared} {
                set terminal_prepared 1
                send -- "stty -echo\r"
            } elseif {!$command_sent} {
                set command_sent 1
                send -- "$remote_command; __wrf_code=\$?; printf '\\n__WRF_BACKEND_COMMAND_DONE__:%s\\n' \"\$__wrf_code\"\r"
            }
        }
        -re {__WRF_BACKEND_COMMAND_DONE__:([0-9]+)\r?\n} {
            set exit_code $expect_out(1,string)
            set output $expect_out(buffer)
            regsub {__WRF_BACKEND_COMMAND_DONE__:[0-9]+\r?\n$} $output "" output
            regsub -all {\r} $output "" output
            puts -nonewline [string trim $output "\n"]
            flush stdout
            send -- "exit\r"
            catch {expect eof}
            exit $exit_code
        }
        eof {
            append transcript $expect_out(buffer)
            puts stderr [string trim $transcript]
            exit 125
        }
        timeout {
            puts stderr "超算交互式命令执行超时"
            exit 124
        }
    }
}
'''
        try:
            result = subprocess.run(
                [expect_bin, "-f", "-", "--", *ssh_command],
                input=script,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise HpcError("超算交互式命令执行超时") from exc
        if result.returncode:
            raise HpcError((result.stderr or result.stdout or "超算命令失败").strip())
        return subprocess.CompletedProcess(
            [*ssh_command, remote_command], 0, result.stdout, result.stderr
        )

    def _run_ssh_with_pexpect(
        self,
        ssh_command: list[str],
        remote_command: str,
        password: str,
        timeout: int,
        pexpect: Any,
    ) -> subprocess.CompletedProcess[str]:
        child = pexpect.spawn(
            ssh_command[0], ssh_command[1:], encoding="utf-8", timeout=timeout
        )
        transcript: list[str] = []
        terminal_prepared = False
        try:
            while True:
                index = child.expect(
                    [
                        r"(?i)password:",
                        "Select server:",
                        "Select account:",
                        r"(?m)[$#]\s*$",
                        pexpect.EOF,
                        pexpect.TIMEOUT,
                    ]
                )
                transcript.append(child.before or "")
                if index == 0:
                    child.sendline(password)
                elif index == 1:
                    child.sendline(os.getenv("WRF_HPC_SERVER_INDEX", "4"))
                elif index == 2:
                    child.sendline(os.getenv("WRF_HPC_ACCOUNT_INDEX", "2"))
                elif index == 3 and not terminal_prepared:
                    terminal_prepared = True
                    child.sendline("stty -echo")
                elif index == 3:
                    wrapper = (
                        f"{remote_command}; __wrf_code=$?; "
                        "printf '\\n__WRF_BACKEND_COMMAND_DONE__:%s\\n' \"$__wrf_code\""
                    )
                    child.sendline(wrapper)
                    outcome = child.expect(
                        [
                            r"__WRF_BACKEND_COMMAND_DONE__:([0-9]+)\r?\n",
                            pexpect.EOF,
                            pexpect.TIMEOUT,
                        ]
                    )
                    output = (child.before or "").replace("\r", "").strip("\n")
                    if outcome != 0:
                        raise HpcError(output or "超算交互式命令未完成")
                    exit_code = int(child.match.group(1))
                    child.sendline("exit")
                    try:
                        child.expect(pexpect.EOF, timeout=5)
                    except (pexpect.EOF, pexpect.TIMEOUT):
                        pass
                    if exit_code:
                        raise HpcError(output or f"超算命令退出码 {exit_code}")
                    return subprocess.CompletedProcess(
                        [*ssh_command, remote_command], 0, output, ""
                    )
                elif index == 4:
                    raise HpcError("".join(transcript).strip() or "超算连接提前关闭")
                else:
                    raise HpcError("超算交互式命令执行超时")
        finally:
            if child.isalive():
                child.close(force=True)

    def _run_process(self, command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        if not self._uses_password():
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
            if result.returncode:
                raise HpcError((result.stderr or result.stdout or "超算命令失败").strip())
            return result
        password = self._password()
        if not password:
            raise HpcError("需要输入超算登录密码")
        try:
            import pexpect
        except ImportError:
            if Path(command[0]).name == "ssh" and len(command) >= 3:
                return self._run_ssh_with_system_expect(
                    command[:-1], command[-1], password, timeout
                )
            return self._run_with_system_expect(command, password, timeout)
        if Path(command[0]).name == "ssh" and len(command) >= 3:
            return self._run_ssh_with_pexpect(
                command[:-1], command[-1], password, timeout, pexpect
            )
        child = pexpect.spawn(command[0], command[1:], encoding="utf-8", timeout=timeout)
        chunks: list[str] = []
        while True:
            index = child.expect([r"(?i)password:", "Select server:", "Select account:", pexpect.EOF, pexpect.TIMEOUT])
            chunks.append(child.before or "")
            if index == 0:
                child.sendline(password)
            elif index == 1:
                child.sendline(os.getenv("WRF_HPC_SERVER_INDEX", "4"))
            elif index == 2:
                child.sendline(os.getenv("WRF_HPC_ACCOUNT_INDEX", "2"))
            elif index == 3:
                break
            else:
                child.close(force=True)
                raise HpcError("超算命令执行超时")
        child.close()
        if child.exitstatus not in (0, None):
            raise HpcError("".join(chunks).strip() or "超算命令失败")
        return subprocess.CompletedProcess(command, child.exitstatus or 0, "".join(chunks), "")

    def authenticate_password(self, password: str) -> dict[str, Any]:
        if not password or not password.strip():
            return {"status": "auth_required", "message": "超算密码不能为空"}
        with self._credential_lock:
            previous = self._session_password
            self._session_password = password
            result = self.health()
            message = str(result.get("message") or "")
            if result.get("status") != "ready" and not message.startswith("缺少:"):
                self._session_password = previous
            return result

    def clear_session_password(self) -> None:
        with self._credential_lock:
            self._session_password = None

    def run(self, remote_command: str, timeout: int = 120) -> str:
        result = self._run_process([*self._ssh_base(), remote_command], timeout=timeout)
        return result.stdout.strip()

    def upload(self, local_path: Path, remote_path: str, timeout: int = 600) -> None:
        self._run_process([*self._scp_base(), str(local_path), f"{self.target}:{remote_path}"], timeout=timeout)

    def download_glob(self, remote_glob: str, local_dir: Path, timeout: int = 1800) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        self._run_process([*self._scp_base(), f"{self.target}:{remote_glob}", str(local_dir)], timeout=timeout)

    def health(self) -> dict[str, Any]:
        try:
            output = self.run(
                f"missing=''; test -d {self.settings.hpc_wps_source_dir} || missing=\"$missing WPS\"; "
                f"test -d {self.settings.hpc_wrf_source_dir} || missing=\"$missing WRF\"; "
                f"test -d {self.settings.hpc_geog_dir} || missing=\"$missing WPS_GEOG\"; "
                "if test -z \"$missing\"; then printf WRF_HPC_READY; else printf '缺少:%s' \"$missing\"; fi",
                timeout=self.settings.hpc_connect_timeout + 5,
            )
            ready = "WRF_HPC_READY" in output
            return {"status": "ready" if ready else "unavailable", "message": output}
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            password_connection_closed = "password:" in lowered and "connection to" in lowered and "closed" in lowered
            if "permission denied" in lowered or password_connection_closed or "需要输入超算登录密码" in message:
                return {"status": "auth_required", "message": "需要输入超算登录密码"}
            return {"status": "unavailable", "message": message}

    def _assert_task_id(self, task_id: str) -> None:
        if not SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("非法任务 ID")

    def service_dir(self) -> str:
        return f"{self.settings.hpc_remote_dir}/backend_wrf_service"

    def task_dir(self, task_id: str) -> str:
        self._assert_task_id(task_id)
        return f"{self.settings.hpc_remote_dir}/backend_wrf_tasks/{task_id}"

    def remote_output_dir(self, task_id: str) -> str:
        self._assert_task_id(task_id)
        return f"{self.settings.hpc_remote_dir}/WRF_{task_id}/run"

    def prepare_runtime(self, task_id: str, config_path: Path, script_dir: Path) -> None:
        service_dir, task_dir = self.service_dir(), self.task_dir(task_id)
        self.run(f"mkdir -p {service_dir} {task_dir}")
        self.upload(script_dir / "wrf.sh", f"{service_dir}/wrf.sh")
        self.upload(script_dir / "wrf_hpc_gfs.sh", f"{service_dir}/wrf_hpc_gfs.sh")
        self.upload(config_path, f"{task_dir}/task.json")
        self.run(f"chmod 700 {service_dir}/wrf.sh {service_dir}/wrf_hpc_gfs.sh")

    def launch(self, task_id: str) -> dict[str, Any]:
        service_dir, task_dir = self.service_dir(), self.task_dir(task_id)
        command = (
            f"cd {task_dir} && "
            f"nohup setsid bash -c {shlex.quote(f'WORK_DIR={self.settings.hpc_remote_dir} WPS_SOURCE_DIR={self.settings.hpc_wps_source_dir} WRF_SOURCE_DIR={self.settings.hpc_wrf_source_dir} GEOG_DATA_PATH={self.settings.hpc_geog_dir} WRF_TASK_CONFIG={task_dir}/task.json WRF_NONINTERACTIVE=true WRF_GFS_DATA_ROOT={self.settings.hpc_gfs_dir} bash {service_dir}/wrf_hpc_gfs.sh; code=$?; echo $code > {task_dir}/exit.code; exit $code')} "
            f"> service.log 2>&1 < /dev/null & echo $! > service.pid; cat service.pid"
        )
        pid = self.run(command).splitlines()[-1]
        if not pid.isdigit():
            raise HpcError("未获得远端 WRF 进程 ID")
        return {"remote_pid": int(pid), "remote_task_dir": task_dir, "remote_output_dir": self.remote_output_dir(task_id)}

    def status(self, task_id: str) -> dict[str, Any]:
        task_dir = self.task_dir(task_id)
        command = (
            f"if test -f {task_dir}/exit.code; then printf 'EXIT:'; cat {task_dir}/exit.code; "
            f"elif test -f {task_dir}/service.pid && kill -0 $(cat {task_dir}/service.pid) 2>/dev/null; then printf RUNNING; "
            f"elif test -f {task_dir}/service.pid; then printf LOST; else printf MISSING; fi"
        )
        state = self.run(command).strip()
        log = self.run(f"tail -n 80 {task_dir}/service.log 2>/dev/null || true", timeout=60)
        if state.startswith("EXIT:0"):
            return {"status": "succeeded", "log": log}
        if state.startswith("EXIT:"):
            return {"status": "failed", "exit_code": state.split(":", 1)[1].strip(), "log": log}
        if state == "RUNNING":
            return {"status": "running", "log": log}
        return {"status": state.lower(), "log": log}

    def download_outputs(self, task_id: str, local_dir: Path) -> None:
        remote = self.remote_output_dir(task_id)
        count = self.run(f"find {remote} -maxdepth 1 -type f -name 'wrfout_d*_*' | wc -l")
        if int(count.splitlines()[-1] or 0) <= 0:
            raise HpcError("超算任务成功但没有找到 wrfout 输出")
        self.download_glob(f"{remote}/wrfout_d*_*", local_dir)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task_dir = self.task_dir(task_id)
        command = (
            f"pid=$(cat {task_dir}/service.pid 2>/dev/null || true); "
            f"case \"$pid\" in ''|*[!0-9]*) printf UNCONFIRMED;; *) "
            f"if kill -0 \"$pid\" 2>/dev/null; then kill -TERM -- -\"$pid\" && printf TERM_SENT; else printf NOT_RUNNING; fi;; esac"
        )
        return {"status": self.run(command).strip().lower()}


# 新实现通过一个持久 pexpect TTY 完成堡垒机菜单导航、命令和文件传输。
from hpc_transport import HpcAuthError, HpcClient, HpcError  # noqa: E402,F401


def build_task_config(task_id: str, request: dict[str, Any], cycle: str, hours: list[int]) -> dict[str, Any]:
    start = request["start_time"]
    end = request["end_time"]
    if isinstance(start, str):
        start = start.replace("Z", "+00:00")
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = end.replace("Z", "+00:00")
        end = datetime.fromisoformat(end)
    spinup = request.get("spinup") or {"mode": "off", "hours": 0}
    if spinup.get("mode") == "custom":
        spinup_hours = int(spinup.get("hours") or 0)
    elif spinup.get("mode") == "auto":
        focus = request.get("forecast_focus", "general")
        finest_dx = min(int(item.get("dx") or 999999) for item in request.get("domains") or [{}])
        base = 12 if focus in {"convection", "urban", "snowfall"} or finest_dx <= 3000 else 6
        interval = int(request.get("forecast_interval_hours") or 1)
        spinup_hours = next((value for value in (6, 12, 18, 24) if value >= base and value % interval == 0), 24)
    else:
        spinup_hours = 0
    model_start = start - timedelta(hours=spinup_hours)
    assimilation = {
        "off": {"grid_fdda": 0, "guv": 0.0, "gt": 0.0, "gq": 0.0},
        "fdda_weak": {"grid_fdda": 1, "guv": 0.0001, "gt": 0.0001, "gq": 0.0},
        "fdda_standard": {"grid_fdda": 1, "guv": 0.0003, "gt": 0.0003, "gq": 0.0},
        "fdda_strong": {"grid_fdda": 1, "guv": 0.0006, "gt": 0.0006, "gq": 0.0},
    }
    scheme = request["assimilation_scheme"]
    return {
        "task_id": task_id,
        "display_id": task_id,
        "data_source": "gfs",
        "time_range": {
            "start_year": model_start.year, "start_month": model_start.month, "start_day": model_start.day, "start_hour": model_start.hour,
            "end_year": end.year, "end_month": end.month, "end_day": end.day, "end_hour": end.hour,
            "product_start": start.isoformat(),
            "model_start": model_start.isoformat(),
            "spinup_hours": spinup_hours,
        },
        "center": request["center"],
        "max_dom": len(request["domains"]),
        "domains": request["domains"],
        "physics": {"num_metgrid_levels": 34, **{key: val for key, val in request["physics"].items() if key != "preset"}},
        "assimilation": {
            "scheme": scheme,
            "params": assimilation[scheme],
            "end_hour": spinup_hours,
            "coarse_min_dx": 9000,
            "ramp_minutes": 60,
        },
        "gfs_cache": {
            "mode": "global_cycle_fhour",
            "gfs_cycle_date": cycle[:8],
            "gfs_cycle_hour": cycle[8:10],
            "gfs_forecast_hours": hours,
            "gfs_required_forecast_hours": hours,
            "gfs_file_interval_hours": request["forecast_interval_hours"],
        },
        "output": {"task_tag": task_id},
    }


def write_task_config(task_id: str, request: dict[str, Any], cycle: str, hours: list[int], path: Path) -> dict[str, Any]:
    value = build_task_config(task_id, request, cycle, hours)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return value


def _runtime_environment(config: dict[str, Any]) -> dict[str, str]:
    time_range = config["time_range"]
    center = config["center"]
    physics = config["physics"]
    gfs = config["gfs_cache"]
    assimilation = config["assimilation"]
    environment: dict[str, Any] = {
        "WRF_TASK_TAG": config["output"]["task_tag"],
        "WRF_DATA_SOURCE": "gfs",
        "WRF_NONINTERACTIVE": "true",
        "WRF_MAX_DOM": config["max_dom"],
        "WRF_START_YEAR": time_range["start_year"],
        "WRF_START_MONTH": f"{int(time_range['start_month']):02d}",
        "WRF_START_DAY": f"{int(time_range['start_day']):02d}",
        "WRF_START_HOUR": f"{int(time_range['start_hour']):02d}",
        "WRF_END_YEAR": time_range["end_year"],
        "WRF_END_MONTH": f"{int(time_range['end_month']):02d}",
        "WRF_END_DAY": f"{int(time_range['end_day']):02d}",
        "WRF_END_HOUR": f"{int(time_range['end_hour']):02d}",
        "WRF_REF_LAT": center["lat"],
        "WRF_REF_LON": center["lon"],
        "WRF_GFS_DATE": gfs["gfs_cycle_date"],
        "WRF_GFS_HOUR": f"{int(gfs['gfs_cycle_hour']):02d}",
        "WRF_GFS_FORECAST_HOURS": " ".join(
            f"{int(hour):03d}" for hour in gfs["gfs_forecast_hours"]
        ),
        "WRF_GFS_CACHE_MODE": gfs["mode"],
        "WRF_GFS_FILE_INTERVAL_HOURS": gfs["gfs_file_interval_hours"],
        "WRF_FORECAST_FILE_INTERVAL_HOURS": gfs["gfs_file_interval_hours"],
        "WRF_ASSIMILATION_SCHEME": assimilation["scheme"],
        "WRF_ASSIM_SPINUP_HOURS": time_range.get("spinup_hours", 0),
        "WRF_HISTORY_BEGIN_MINUTES": int(time_range.get("spinup_hours", 0)) * 60,
    }
    physics_mapping = {
        "num_metgrid_levels": "WRF_NUM_METGRID_LEVELS",
        "mp_physics": "WRF_MP_PHYSICS",
        "cu_physics": "WRF_CU_PHYSICS",
        "ra_lw_physics": "WRF_RA_LW_PHYSICS",
        "ra_sw_physics": "WRF_RA_SW_PHYSICS",
        "bl_pbl_physics": "WRF_BL_PBL_PHYSICS",
        "sf_sfclay_physics": "WRF_SF_SFCLAY_PHYSICS",
        "sf_surface_physics": "WRF_SF_SURFACE_PHYSICS",
        "sf_urban_physics": "WRF_SF_URBAN_PHYSICS",
        "num_soil_layers": "WRF_NUM_SOIL_LAYERS",
        "num_land_cat": "WRF_NUM_LAND_CAT",
        "radt": "WRF_RADT",
    }
    for key, variable in physics_mapping.items():
        if key in physics:
            environment[variable] = physics[key]
    if physics.get("cu_physics_by_domain"):
        environment["WRF_CU_PHYSICS_BY_DOMAIN"] = ", ".join(
            str(int(value)) for value in physics["cu_physics_by_domain"]
        )
    if physics.get("sf_urban_physics_by_domain"):
        environment["WRF_SF_URBAN_PHYSICS_BY_DOMAIN"] = ", ".join(
            str(int(value)) for value in physics["sf_urban_physics_by_domain"]
        )
    for key, value in assimilation["params"].items():
        environment[f"WRF_ASSIM_{key.upper()}"] = value
    for index, domain in enumerate(config["domains"], 1):
        suffix = f"D{index:02d}"
        for key, variable in (
            ("dx", "WRF_DX"),
            ("dy", "WRF_DY"),
            ("e_we", "WRF_E_WE"),
            ("e_sn", "WRF_E_SN"),
            ("i_parent_start", "WRF_I_PARENT_START"),
            ("j_parent_start", "WRF_J_PARENT_START"),
        ):
            environment[f"{variable}_{suffix}"] = domain[key]
        if index == 1:
            # wrf.sh 用无后缀外层网格距计算稳定时间步长。
            environment["WRF_DX"] = domain["dx"]
            environment["WRF_DY"] = domain["dy"]
    return {name: str(value) for name, value in environment.items()}


def _write_runtime_environment(config: dict[str, Any], path: Path) -> None:
    lines = [
        f"export {name}={shlex.quote(value)}"
        for name, value in sorted(_runtime_environment(config).items())
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _write_expected_gfs_index(
    cycle: str,
    hours: list[int],
    entries: list[dict[str, Any]],
    path: Path,
) -> None:
    requested = sorted(set(int(hour) for hour in hours))
    by_hour: dict[int, tuple[str, int, str]] = {}
    for item in entries:
        name = str(item.get("name") or "")
        match = SAFE_GFS_NAME.fullmatch(name)
        digest = str(item.get("sha256") or "").lower()
        try:
            forecast_hour = int(item.get("forecast_hour"))
            size = int(item.get("size"))
        except (TypeError, ValueError):
            continue
        if (
            match is None
            or match.group("cycle_hour") != cycle[8:10]
            or int(match.group("forecast_hour")) != forecast_hour
            or size <= 0
            or SHA256.fullmatch(digest) is None
        ):
            continue
        by_hour[forecast_hour] = (name, size, digest)
    missing = [hour for hour in requested if hour not in by_hour]
    if missing:
        raise HpcError(f"缺少已验证的远端 GFS 索引：{missing}")
    lines = [
        f"{by_hour[hour][0]}\t{by_hour[hour][1]}\t{by_hour[hour][2]}\t{hour:03d}"
        for hour in requested
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def write_task_bundle(
    task_id: str,
    request: dict[str, Any],
    cycle: str,
    hours: list[int],
    entries: list[dict[str, Any]],
    config_path: Path,
    environment_path: Path,
    expected_gfs_path: Path,
) -> None:
    config = write_task_config(task_id, request, cycle, hours, config_path)
    _write_runtime_environment(config, environment_path)
    _write_expected_gfs_index(cycle, hours, entries, expected_gfs_path)
