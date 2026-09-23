from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
import threading
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Callable

from config import Settings


SAFE_TASK_ID = re.compile(r"^(?:wrf_(?:(?:gfs|ecmwf)_)?[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}|wrf_agent_[0-9a-f]{32})$")
SAFE_DOWNSCALE_TASK_ID = re.compile(r"^ds_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
SAFE_CYCLE = re.compile(r"^[0-9]{10}$")
SAFE_REMOTE_ROOT = re.compile(r"^(?:~|/)[^\s;|&<>`$(){}\[\]\\\"']*$")
SAFE_GFS_NAME = re.compile(r"^gfs\.t[0-9]{2}z\.pgrb2\.0p25\.f([0-9]{3})$")
SAFE_EC_NAME = re.compile(r"^ecmwf\.t[0-9]{2}z\.ifs\.0p25\.f([0-9]{3})\.grib2$")
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9_.*?+:-]+$")
SAFE_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
SAFE_STORAGE_UUID = re.compile(r"^[0-9A-Fa-f]{16,64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MEBIBYTE = 1024 * 1024
GFS_PRODUCT = "gfs-0p25-global"
GFS_SCOPE = "global"
EC_PRODUCT = "ecmwf-ifs-0p25"
EC_SCOPE = "global"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_local_output_name(name: str) -> str:
    """Return a wrfout file name that is safe on both Linux and Windows."""
    return name.replace(":", "-")


class HpcError(RuntimeError):
    pass


class HpcStorageError(HpcError):
    """远端 WRF 数据卷未挂载、布局异常或稳定别名无法建立。"""


class HpcAuthError(HpcError):
    pass


class HpcSessionStaleError(HpcError):
    """命令尚未下发，但持久会话已经不在计算节点 Shell。"""


class HpcClient:
    """WRF 远端传输客户端；tx-lab 使用密钥直连，旧堡垒机模式仅保留兼容。"""

    # 复用 wrfautosystem.HpcSession 的堡垒机入口和提示符约定。
    WRFAUTO_PASSWORD_PROMPT = "(xjm_shaoyongjin@202.195.239.23) Password:"
    SHELL_PROMPTS = ["$", "#"]

    def __init__(self, settings: Settings):
        self.settings = settings
        self._credential_lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._session_password: str | None = None
        self._child: Any | None = None
        self._shell_ready = False
        self._sftp_disabled_reason: str | None = None
        self._connection_diagnostic: dict[str, Any] = {
            "stage": "idle",
            "attempt": 0,
            "max_attempts": settings.hpc_connect_retries,
            "detail": None,
        }
        self._transfer_status: dict[str, str] = {
            "mode": "pending",
            "state": "idle",
            "message": "等待传输运行文件",
        }
        if settings.hpc_connection_mode not in {"bastion", "direct"}:
            raise ValueError("WRF_HPC_CONNECTION_MODE 必须是 bastion 或 direct")
        if settings.hpc_transfer_mode not in {"auto", "sftp", "pty"}:
            raise ValueError("WRF_HPC_TRANSFER_MODE 必须是 auto、sftp 或 pty")
        if not SAFE_STORAGE_UUID.fullmatch(settings.hpc_storage_uuid):
            raise ValueError("WRF_HPC_STORAGE_UUID 格式无效")
        for label, value in (
            ("WRF_HPC_STORAGE_ALIAS", settings.hpc_storage_alias),
            ("WRF_HPC_REMOTE_DIR", settings.hpc_remote_dir),
            ("WRF_HPC_GFS_DIR", settings.hpc_gfs_dir),
            ("WRF_HPC_EC_DIR", settings.hpc_ec_dir),
            ("WRF_HPC_WPS_SOURCE_DIR", settings.hpc_wps_source_dir),
            ("WRF_HPC_WRF_SOURCE_DIR", settings.hpc_wrf_source_dir),
            ("WRF_HPC_GEOG_DATA_PATH", settings.hpc_geog_dir),
            ("WRF_HPC_GFS_DOWNLOAD_SCRIPT", settings.hpc_gfs_download_script),
            ("WRF_HPC_RUNTIME_ENV", settings.hpc_runtime_env),
            ("WRF_HPC_DOWNSCALE_INTERFACE_ROOT", settings.hpc_downscale_interface_root),
            ("WRF_HPC_DOWNSCALE_CFD_TEMPLATE", settings.hpc_downscale_cfd_template),
            ("WRF_HPC_DOWNSCALE_OPENFOAM_ACTIVATE", settings.hpc_downscale_openfoam_activate),
            ("WRF_HPC_DOWNSCALE_PYTHON", settings.hpc_downscale_python),
            ("WRF_HPC_GFS_MOUNT", settings.hpc_gfs_mount),
        ):
            if not SAFE_REMOTE_ROOT.fullmatch(value):
                raise ValueError(f"{label} 包含不安全字符")

    @property
    def target(self) -> str:
        if self.settings.hpc_user:
            return f"{self.settings.hpc_user}@{self.settings.hpc_host}"
        return self.settings.hpc_host

    @property
    def gfs_bounds(self) -> list[float]:
        return [-180.0, -90.0, 180.0, 90.0]

    @staticmethod
    def _pexpect() -> Any:
        try:
            import pexpect
        except ImportError as exc:
            raise HpcError("缺少 pexpect，运行 ./start.sh 可自动安装") from exc
        return pexpect

    def _uses_password(self) -> bool:
        with self._credential_lock:
            return self._session_password is not None or self.settings.hpc_auth_mode == "password"

    def _password(self) -> str:
        with self._credential_lock:
            if self._session_password is not None:
                return self._session_password
            return self.settings.hpc_password

    def _uses_session_transport(self) -> bool:
        return self.settings.hpc_connection_mode == "bastion" or self._uses_password()

    def _ssh_base(self, *, tty: bool = False) -> list[str]:
        command = [
            "ssh", "-p", str(self.settings.hpc_port),
            "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
        ]
        if self.settings.hpc_connection_mode == "direct":
            command.extend(["-o", "StrictHostKeyChecking=yes"])
            if self.settings.hpc_known_hosts_file:
                command.extend(["-o", f"UserKnownHostsFile={self.settings.hpc_known_hosts_file}"])
        else:
            command.extend(["-o", "StrictHostKeyChecking=no", "-o", "HostKeyAlgorithms=+ssh-rsa"])
        if tty:
            command.append("-tt")
        if self._uses_password():
            command.extend([
                "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no",
            ])
        elif self.settings.hpc_auth_mode == "key":
            command.extend(["-o", "BatchMode=yes"])
        if self.settings.hpc_key_file:
            command.extend(["-i", self.settings.hpc_key_file])
        command.append(self.target)
        return command

    def _scp_base(self) -> list[str]:
        command = [
            "scp", "-q", "-P", str(self.settings.hpc_port),
            "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}",
        ]
        if self.settings.hpc_connection_mode == "direct":
            command.extend(["-o", "StrictHostKeyChecking=yes"])
            if self.settings.hpc_known_hosts_file:
                command.extend(["-o", f"UserKnownHostsFile={self.settings.hpc_known_hosts_file}"])
        else:
            command.extend(["-o", "StrictHostKeyChecking=no", "-o", "HostKeyAlgorithms=+ssh-rsa"])
        if self.settings.hpc_auth_mode == "key":
            command.extend(["-o", "BatchMode=yes"])
        if self.settings.hpc_key_file:
            command.extend(["-i", self.settings.hpc_key_file])
        return command

    def _sftp_base(self) -> list[str]:
        command = [
            "sftp", "-P", str(self.settings.hpc_port),
            "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
        ]
        if self.settings.hpc_connection_mode == "direct":
            command.extend(["-o", "StrictHostKeyChecking=yes"])
            if self.settings.hpc_known_hosts_file:
                command.extend(["-o", f"UserKnownHostsFile={self.settings.hpc_known_hosts_file}"])
        else:
            command.extend(["-o", "StrictHostKeyChecking=no", "-o", "HostKeyAlgorithms=+ssh-rsa"])
        if self._uses_password():
            command.extend([
                "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no",
            ])
        elif self.settings.hpc_auth_mode == "key":
            command.extend(["-o", "BatchMode=yes"])
        if self.settings.hpc_key_file:
            command.extend(["-i", self.settings.hpc_key_file])
        command.append(self.target)
        return command

    @property
    def transfer_status(self) -> dict[str, str]:
        return dict(self._transfer_status)

    @staticmethod
    def _quote_remote(path: str) -> str:
        if path == "~":
            return "~"
        if path.startswith("~/"):
            return "~/" + shlex.quote(path[2:])
        return shlex.quote(path)

    @staticmethod
    def _split_marker(marker: str) -> tuple[str, str]:
        midpoint = max(1, len(marker) // 2)
        return marker[:midpoint], marker[midpoint:]

    @classmethod
    def _marker_command(cls, marker: str, *, disable_echo: bool = False) -> str:
        left, right = cls._split_marker(marker)
        prefix = "stty -echo 2>/dev/null; " if disable_echo else ""
        return (
            f"{prefix}printf '\\n%s%s\\n' "
            f"{shlex.quote(left)} {shlex.quote(right)}"
        )

    def _sanitize_terminal_text(self, *values: Any, limit: int = 800) -> str | None:
        text = "\n".join(value for value in values if isinstance(value, str) and value)
        if not text:
            return None
        text = ANSI_ESCAPE.sub("", text).replace("\r", "\n")
        text = "".join(character if character in "\n\t" or ord(character) >= 32 else " " for character in text)
        with self._credential_lock:
            secrets = [self._session_password, self.settings.hpc_password]
        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")
        lines = [" ".join(line.split()) for line in text.splitlines()]
        text = "\n".join(line for line in lines if line).strip()
        if not text:
            return None
        return text[-limit:]

    def _set_connection_diagnostic(
        self,
        *,
        stage: str,
        attempt: int,
        detail: str | None = None,
    ) -> None:
        self._connection_diagnostic = {
            "stage": stage,
            "attempt": attempt,
            "max_attempts": self.settings.hpc_connect_retries,
            "detail": detail,
        }

    @property
    def connection_diagnostic(self) -> dict[str, Any]:
        return dict(self._connection_diagnostic)

    def _close_session_locked(self) -> None:
        child, self._child = self._child, None
        self._shell_ready = False
        if child is not None:
            try:
                child.close(force=True)
            except Exception:
                pass

    def close_session(self) -> None:
        with self._session_lock:
            self._close_session_locked()

    def _session_alive_locked(self) -> bool:
        if self._child is None:
            return False
        try:
            return bool(self._child.isalive()) and not bool(self._child.eof())
        except Exception:
            return False

    def _connect_session_once_locked(self, attempt: int) -> None:
        pexpect = self._pexpect()
        self._shell_ready = False
        # 与 wrfautosystem 完全一致：使用本机 SSH Host 别名，不拼接 user@。
        ssh_command = (
            "ssh -tt -o StrictHostKeyChecking=no "
            f"-o HostKeyAlgorithms=+ssh-rsa {shlex.quote(self.settings.hpc_host)}"
        )
        child = pexpect.spawn(
            "/bin/bash",
            ["-c", ssh_command],
            encoding="utf-8",
            codec_errors="replace",
            timeout=self.settings.hpc_connect_timeout,
            maxread=65536,
        )
        child.delaybeforesend = 0.05
        self._child = child
        password = self._password()
        self._set_connection_diagnostic(stage="堡垒机登录", attempt=attempt)

        index = child.expect_exact(
            [
                self.WRFAUTO_PASSWORD_PROMPT,
                "password:",
                "Password:",
                "Select server:",
                "$",
                "#",
                "Permission denied",
                pexpect.EOF,
                pexpect.TIMEOUT,
            ],
            timeout=self.settings.hpc_connect_timeout,
        )
        if index in {0, 1, 2}:
            if not password:
                raise HpcAuthError("需要输入超算登录密码")
            self._set_connection_diagnostic(stage="密码认证", attempt=attempt)
            child.sendline(password)
            index = child.expect_exact(
                [
                    "Select server:",
                    "$",
                    "#",
                    self.WRFAUTO_PASSWORD_PROMPT,
                    "password:",
                    "Password:",
                    "Permission denied",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=self.settings.hpc_connect_timeout,
            )
            if index in {3, 4, 5, 6}:
                raise HpcAuthError("超算密码认证失败")
            if index in {7, 8}:
                raise HpcError("堡垒机连接在认证后断开或超时")
            index = 3 if index == 0 else index + 3

        if index == 3:
            self._set_connection_diagnostic(stage="服务器菜单", attempt=attempt)
            child.sendline(self.settings.hpc_server_index)
            server_result = child.expect_exact(
                ["Select account:", "$", "#", pexpect.EOF, pexpect.TIMEOUT],
                timeout=15,
            )
            if server_result == 0:
                self._set_connection_diagnostic(stage="账号菜单", attempt=attempt)
                child.sendline(self.settings.hpc_account_index)
                shell_result = child.expect_exact(
                    [
                        "$",
                        "#",
                        self.WRFAUTO_PASSWORD_PROMPT,
                        "password:",
                        "Password:",
                        pexpect.EOF,
                        pexpect.TIMEOUT,
                    ],
                    timeout=self.settings.hpc_shell_ready_timeout,
                )
                if shell_result in {2, 3, 4}:
                    if not password:
                        raise HpcAuthError("需要输入计算节点密码")
                    self._set_connection_diagnostic(
                        stage="计算节点密码认证",
                        attempt=attempt,
                    )
                    child.sendline(password)
                    shell_result = child.expect_exact(
                        ["$", "#", "Permission denied", pexpect.EOF, pexpect.TIMEOUT],
                        timeout=self.settings.hpc_shell_ready_timeout,
                    )
                    if shell_result == 2:
                        raise HpcAuthError("计算节点密码认证失败")
                if shell_result not in {0, 1}:
                    raise HpcError("计算节点 Shell 初始化失败")
            elif server_result not in {1, 2}:
                raise HpcError("堡垒机账号菜单初始化失败")
        elif index not in {4, 5}:
            detail = self._sanitize_terminal_text(child.before, child.after)
            self._set_connection_diagnostic(
                stage="堡垒机登录",
                attempt=attempt,
                detail=detail,
            )
            if index == 6:
                raise HpcAuthError("超算密码认证失败")
            raise HpcError("未进入堡垒机菜单或计算节点 Shell")

        self._shell_ready = True
        self._set_connection_diagnostic(stage="ready", attempt=attempt)

    def _connect_session_locked(self) -> None:
        if self._session_alive_locked() and self._shell_ready:
            return
        if self._session_alive_locked():
            self._close_session_locked()
        attempts = self.settings.hpc_connect_retries
        last_error: HpcError | None = None
        pexpect = self._pexpect()
        for attempt in range(1, attempts + 1):
            self._close_session_locked()
            try:
                self._connect_session_once_locked(attempt)
                return
            except HpcAuthError:
                self._close_session_locked()
                raise
            except HpcError as exc:
                last_error = exc
                self._close_session_locked()
            except Exception as exc:
                self._close_session_locked()
                if not isinstance(exc, pexpect.ExceptionPexpect):
                    raise
                last_error = HpcError("堡垒机交互超时或连接中断")
            if attempt >= attempts:
                diagnostic = self.connection_diagnostic
                detail = diagnostic.get("detail")
                suffix = f"；节点返回：{detail}" if detail else ""
                raise HpcError(
                    f"{last_error}（尝试 {attempt}/{attempts}）{suffix}"
                ) from None
            delay = self.settings.hpc_connect_retry_delay_seconds * attempt
            if delay > 0:
                time.sleep(delay)
        raise last_error or HpcError("超算会话初始化失败")

    def _run_session_locked(self, remote_command: str, timeout: int) -> str:
        self._connect_session_locked()
        pexpect = self._pexpect()
        child = self._child
        marker = f"___CMD_DONE_{uuid.uuid4().hex[:8]}___"
        pre_marker = f"___PRE_{uuid.uuid4().hex[:8]}___"
        command_dispatched = False
        try:
            # 沿用 wrfautosystem 的 pre-marker + 提示符清空方式，避免命令回显
            # 或登录横幅被误当成远端命令输出。
            child.sendline(f"stty -echo; echo {pre_marker}")
            started = child.expect_exact(
                [
                    pre_marker,
                    "Select server:",
                    "Select account:",
                    self.WRFAUTO_PASSWORD_PROMPT,
                    "password:",
                    "Password:",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=5,
            )
            if started != 0:
                detail = self._sanitize_terminal_text(child.before, child.after)
                self._set_connection_diagnostic(stage="会话重建", attempt=0, detail=detail)
                self._close_session_locked()
                raise HpcSessionStaleError("超算会话已离开计算节点 Shell")
            prompt = child.expect_exact(
                [
                    *self.SHELL_PROMPTS,
                    "Select server:",
                    "Select account:",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=5,
            )
            if prompt not in {0, 1}:
                detail = self._sanitize_terminal_text(child.before, child.after)
                self._set_connection_diagnostic(stage="会话重建", attempt=0, detail=detail)
                self._close_session_locked()
                raise HpcSessionStaleError("超算会话 Shell 探针失败")

            child.sendline(
                f"( {remote_command}; ); __wrf_ec=$?; "
                f"printf 'EC:%s\\n' \"$__wrf_ec\"; echo {marker}"
            )
            command_dispatched = True
            finished = child.expect_exact(
                [marker, pexpect.EOF, pexpect.TIMEOUT],
                timeout=timeout,
            )
            if finished == 1:
                self._close_session_locked()
                raise HpcError("超算会话在命令执行期间断开")
            if finished == 2:
                self._close_session_locked()
                raise HpcError("超算命令执行超时，未自动重放以避免重复执行")
            raw = (child.before or "").replace("\r", "")
            exit_match = re.search(r"EC:([0-9]+)", raw)
            exit_code = int(exit_match.group(1)) if exit_match else 0
            output = re.sub(r"EC:[0-9]+\s*", "", raw).strip()
            child.sendline("stty echo")
            if exit_code:
                if re.search(r"WRF_STORAGE_(?:MOUNT_MISSING|LAYOUT_MISSING|ALIAS_CONFLICT)", output):
                    raise HpcStorageError(output or f"超算存储初始化退出码 {exit_code}")
                raise HpcError(output or f"超算命令退出码 {exit_code}")
            return output
        except HpcSessionStaleError:
            raise
        except Exception as exc:
            pexpect_error = getattr(pexpect, "ExceptionPexpect", ())
            if pexpect_error and isinstance(exc, pexpect_error):
                self._close_session_locked()
                if not command_dispatched:
                    raise HpcSessionStaleError("超算会话 Shell 探针超时") from None
                raise HpcError("超算会话交互失败，命令未自动重放") from None
            try:
                child.sendline("stty echo")
            except Exception:
                pass
            if not self._session_alive_locked():
                self._close_session_locked()
            raise

    def _run_direct(self, command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HpcError("tx-lab 命令执行超时") from exc
        if result.returncode:
            message = (result.stderr or result.stdout or "超算命令失败").strip()
            if re.search(r"WRF_STORAGE_(?:MOUNT_MISSING|LAYOUT_MISSING|ALIAS_CONFLICT)", message):
                raise HpcStorageError(message)
            if "permission denied" in message.lower():
                if self.settings.hpc_auth_mode == "key":
                    raise HpcAuthError("tx-lab SSH 密钥认证失败，请安装或检查专用公钥")
                raise HpcAuthError("需要输入超算登录密码")
            raise HpcError(message)
        return result

    def authenticate_password(self, password: str) -> dict[str, Any]:
        if self.settings.hpc_connection_mode == "direct" and self.settings.hpc_auth_mode == "key":
            return {
                "status": "auth_required",
                "message": "tx-lab 工作台使用专用 SSH 密钥；密码只允许人工 SSH 登录，不由服务接收",
            }
        if not password or not password.strip():
            return {"status": "auth_required", "message": "超算密码不能为空"}
        with self._credential_lock:
            previous = self._session_password
            self._session_password = password
        self.close_session()
        result = self.health()
        message = str(result.get("message") or "")
        if result.get("status") == "auth_required":
            with self._credential_lock:
                self._session_password = previous
            self.close_session()
        return result

    def clear_session_password(self) -> None:
        self.close_session()
        with self._credential_lock:
            self._session_password = None

    def run(self, remote_command: str, timeout: int = 120) -> str:
        storage_uuid = shlex.quote(self.settings.hpc_storage_uuid)
        storage_alias = self._quote_remote(self.settings.hpc_storage_alias)
        storage_bootstrap = (
            f"storage_mount_escaped=$(findmnt -rn -S UUID={storage_uuid} -o TARGET 2>/dev/null | head -n 1); "
            "storage_mount=$(printf '%b' \"$storage_mount_escaped\"); "
            f"if test -z \"$storage_mount\"; then printf 'WRF_STORAGE_MOUNT_MISSING|uuid={self.settings.hpc_storage_uuid}'; exit 23; fi; "
            f"if ! test -d \"$storage_mount/WRFwork\"; then printf 'WRF_STORAGE_LAYOUT_MISSING|mount=%s' \"$storage_mount\"; exit 24; fi; "
            f"if ! test -f \"$storage_mount/WRFwork/.wrf-data-root\"; then printf 'WRF_STORAGE_LAYOUT_MISSING|marker=%s' \"$storage_mount/WRFwork/.wrf-data-root\"; exit 24; fi; "
            f"if test -L {storage_alias}; then ln -sfn -- \"$storage_mount\" {storage_alias} 2>/dev/null || exit 25; "
            f"elif test -e {storage_alias}; then printf 'WRF_STORAGE_ALIAS_CONFLICT|path={self.settings.hpc_storage_alias}'; exit 26; "
            f"else ln -s -- \"$storage_mount\" {storage_alias} 2>/dev/null || exit 25; fi; "
        )
        remote_command = storage_bootstrap + remote_command
        if self._uses_session_transport():
            with self._session_lock:
                for attempt in range(2):
                    try:
                        return self._run_session_locked(remote_command, timeout).strip()
                    except HpcSessionStaleError:
                        self._close_session_locked()
                        if attempt == 1:
                            raise HpcError("超算会话重建后仍未进入计算节点 Shell") from None
                raise HpcError("超算会话重建失败")
        return self._run_direct([*self._ssh_base(), remote_command], timeout).stdout.strip()

    def _remote_meta(self, remote_path: str, timeout: int = 120) -> tuple[int, str] | None:
        quoted = self._quote_remote(remote_path)
        output = self.run(
            f"if test -f {quoted}; then printf 'SIZE:%s SHA:' \"$(wc -c < {quoted})\"; "
            f"sha256sum {quoted} | awk '{{print $1}}'; else printf MISSING; fi",
            timeout=timeout,
        )
        match = re.search(r"SIZE:([0-9]+) SHA:([0-9a-fA-F]{64})", output)
        if not match:
            return None
        return int(match.group(1)), match.group(2).lower()

    def _send_all_locked(self, value: str, write_size: int = 8192) -> None:
        if not self._session_alive_locked():
            raise HpcError("超算会话已断开")
        child = self._child
        original_delay = child.delaybeforesend
        child.delaybeforesend = None
        try:
            offset = 0
            while offset < len(value):
                written = child.send(value[offset:offset + write_size])
                if not written:
                    raise HpcError("超算 PTY 写入中断")
                offset += written
        finally:
            child.delaybeforesend = original_delay

    def _upload_bytes_session_locked(
        self,
        source: BinaryIO,
        size: int,
        digest: str,
        remote_path: str,
        timeout: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self._connect_session_locked()
        parent = posixpath.dirname(remote_path) or "."
        remote = self._quote_remote(remote_path)
        temporary_path = remote_path + ".upload.part"
        temporary = self._quote_remote(temporary_path)
        self._run_session_locked(f"mkdir -p {self._quote_remote(parent)}", 60)
        completed = self._remote_meta(remote_path, timeout=120)
        if completed == (size, digest):
            if progress:
                progress(size, size)
            return
        self._run_session_locked(f"touch {temporary}", 60)
        partial = self._remote_meta(temporary_path, timeout=120)
        offset = partial[0] if partial else 0
        if offset > size or (offset == size and partial != (size, digest)):
            self._run_session_locked(f": > {temporary}", 60)
            offset = 0
        if offset == size and partial == (size, digest):
            self._run_session_locked(f"mv {temporary} {remote}", 60)
            if progress:
                progress(size, size)
            return

        source.seek(offset)
        if progress:
            progress(offset, size)
        pexpect = self._pexpect()
        while offset < size:
            chunk_size = self.settings.hpc_transfer_chunk_kb * 1024
            data = source.read(min(chunk_size, size - offset))
            if not data:
                raise HpcError("本地上传文件在传输期间提前结束")
            encoded = "\n".join(textwrap.wrap(base64.b64encode(data).decode("ascii"), 76))
            delimiter = f"WRF_B64_{uuid.uuid4().hex}"
            marker = f"__WRF_UPLOAD_CHUNK_{uuid.uuid4().hex}__"
            payload = (
                f"base64 -d >> {temporary} <<'{delimiter}'\n"
                f"{encoded}\n{delimiter}\n"
                f"printf '\\n{marker}\\n'\n"
            )
            self._send_all_locked(payload)
            outcome = self._child.expect_exact(
                [marker, pexpect.EOF, pexpect.TIMEOUT],
                timeout=min(timeout, self.settings.hpc_transfer_chunk_timeout),
            )
            if outcome != 0:
                self._close_session_locked()
                raise HpcError("超算文件分块上传中断，远端断点已保留")
            offset += len(data)
            if progress:
                progress(offset, size)

        uploaded = self._remote_meta(temporary_path, timeout=max(120, timeout))
        if uploaded != (size, digest):
            raise HpcError("超算文件上传完成但大小或 SHA256 校验失败")
        self._run_session_locked(f"mv {temporary} {remote}", 120)

    @staticmethod
    def _sftp_quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _absolute_remote_path(self, remote_path: str) -> str:
        if remote_path == "~" or remote_path.startswith("~/"):
            home = self.run("printf '%s' \"$HOME\"", timeout=60).strip()
            if not home.startswith("/"):
                raise HpcError("无法获取超算账户 HOME 绝对路径")
            suffix = remote_path[2:] if remote_path.startswith("~/") else ""
            return posixpath.join(home, suffix)
        return remote_path

    def _upload_sftp(
        self,
        local_path: Path,
        remote_path: str,
        timeout: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        remote_path = self._absolute_remote_path(remote_path)
        temporary_path = remote_path + ".upload.part"
        size = local_path.stat().st_size
        digest = _sha256(local_path)
        completed = self._remote_meta(remote_path, timeout=120)
        if completed == (size, digest):
            if progress:
                progress(size, size)
            return
        partial = self._remote_meta(temporary_path, timeout=120)
        if partial and (partial[0] > size or (partial[0] == size and partial[1] != digest)):
            self.run(f": > {self._quote_remote(temporary_path)}", timeout=60)
            partial = None

        pexpect = self._pexpect()
        command = self._sftp_base()
        child = pexpect.spawn(
            command[0],
            command[1:],
            encoding="utf-8",
            codec_errors="replace",
            timeout=self.settings.hpc_connect_timeout,
            maxread=65536,
        )
        child.delaybeforesend = 0.02
        password_sent = False
        try:
            while True:
                index = child.expect(
                    [
                        r"(?i)password\s*:",
                        r"Select server\s*:",
                        r"Select account\s*:",
                        r"(?i)are you sure you want to continue connecting",
                        r"(?m)sftp>\s*$",
                        r"(?i)permission denied|authentication failed",
                        r"(?i)connection (?:closed|refused)|subsystem request failed",
                        pexpect.EOF,
                        pexpect.TIMEOUT,
                    ],
                    timeout=self.settings.hpc_connect_timeout,
                )
                if index == 0:
                    password = self._password()
                    if password_sent or not password:
                        raise HpcAuthError("需要输入超算登录密码")
                    child.sendline(password)
                    password_sent = True
                elif index == 1:
                    child.sendline(self.settings.hpc_server_index)
                elif index == 2:
                    child.sendline(self.settings.hpc_account_index)
                elif index == 3:
                    child.sendline("yes")
                elif index == 4:
                    break
                elif index == 5:
                    raise HpcAuthError("超算 SFTP 密码认证失败")
                elif index == 6:
                    raise HpcError("堡垒机 SFTP 子系统不可用")
                elif index == 7:
                    raise HpcError("超算 SFTP 连接提前关闭")
                else:
                    raise HpcError("超算 SFTP 连接超时")

            child.sendline(
                f"reput {self._sftp_quote(str(local_path))} "
                f"{self._sftp_quote(temporary_path)}"
            )
            if progress:
                progress(partial[0] if partial else 0, size)
            while True:
                outcome = child.expect(
                    [
                        r"(?m)sftp>\s*$",
                        r"([0-9]{1,3})%",
                        r"(?i)(?:no such file|permission denied|failure|not found)",
                        pexpect.EOF,
                        pexpect.TIMEOUT,
                    ],
                    timeout=timeout,
                )
                if outcome == 0:
                    break
                if outcome == 1:
                    if progress:
                        percent = max(0, min(100, int(child.match.group(1))))
                        progress(int(size * percent / 100), size)
                    continue
                if outcome == 2:
                    raise HpcError(f"超算 SFTP 上传失败：{child.match.group(0)}")
                if outcome == 3:
                    raise HpcError("超算 SFTP 上传期间连接断开")
                raise HpcError("超算 SFTP 上传超时，远程断点已保留")
            child.sendline("bye")
            try:
                child.expect(pexpect.EOF, timeout=5)
            except (pexpect.EOF, pexpect.TIMEOUT):
                pass
        finally:
            if child.isalive():
                child.close(force=True)

        uploaded = self._remote_meta(temporary_path, timeout=max(120, timeout))
        if uploaded != (size, digest):
            raise HpcError("超算 SFTP 上传完成但大小或 SHA256 校验失败")
        self.run(
            f"mv {self._quote_remote(temporary_path)} {self._quote_remote(remote_path)}",
            timeout=120,
        )
        if progress:
            progress(size, size)

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        timeout: int = 600,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        local_path = Path(local_path)
        if self._uses_session_transport():
            fallback_reason = self._sftp_disabled_reason
            if (
                self.settings.hpc_transfer_mode in {"auto", "sftp"}
                and self._sftp_disabled_reason is None
            ):
                self._transfer_status = {
                    "mode": "sftp",
                    "state": "running",
                    "message": "正在通过堡垒机原生 SFTP 断点传输",
                }
                try:
                    self._upload_sftp(local_path, remote_path, timeout, progress)
                    self._transfer_status = {
                        "mode": "sftp",
                        "state": "succeeded",
                        "message": "堡垒机原生 SFTP 传输成功",
                    }
                    return
                except HpcError as exc:
                    if self.settings.hpc_transfer_mode == "sftp":
                        self._transfer_status = {
                            "mode": "sftp",
                            "state": "failed",
                            "message": str(exc).replace("\n", " ")[:240],
                        }
                        raise
                    self._sftp_disabled_reason = str(exc).replace("\n", " ")[:240]
                    fallback_reason = self._sftp_disabled_reason
            if fallback_reason:
                self._transfer_status = {
                    "mode": "pty_fallback",
                    "state": "running",
                    "message": f"原生 SFTP 不可用，正在通过 PTY 断点传输：{fallback_reason}",
                }
            else:
                self._transfer_status = {
                    "mode": "pty",
                    "state": "running",
                    "message": "正在通过堡垒机 PTY 分块断点传输",
                }
            size = local_path.stat().st_size
            digest = _sha256(local_path)
            maximum = self.settings.hpc_transfer_retries
            for failed_attempts in range(maximum + 1):
                try:
                    with self._session_lock, local_path.open("rb") as source:
                        self._upload_bytes_session_locked(
                            source,
                            size,
                            digest,
                            remote_path,
                            timeout,
                            progress,
                        )
                    if failed_attempts:
                        self._transfer_status = {
                            "mode": "pty_resumed",
                            "state": "succeeded",
                            "message": f"PTY 重连续传成功（第 {failed_attempts} 次重试）",
                        }
                    elif fallback_reason:
                        self._transfer_status = {
                            "mode": "pty_fallback",
                            "state": "succeeded",
                            "message": "原生 SFTP 不可用，PTY 回退传输成功",
                        }
                    else:
                        self._transfer_status = {
                            "mode": "pty",
                            "state": "succeeded",
                            "message": "堡垒机 PTY 分块断点传输成功",
                        }
                    return
                except HpcError as exc:
                    self.close_session()
                    if failed_attempts >= maximum:
                        self._transfer_status = {
                            "mode": "pty_fallback" if fallback_reason else "pty",
                            "state": "failed",
                            "message": f"PTY 断点续传重试耗尽（{maximum} 次）：{exc}",
                        }
                        raise HpcError(
                            f"PTY 断点续传重试耗尽（{maximum} 次）：{exc}"
                        ) from exc
                    attempt = failed_attempts + 1
                    self._transfer_status = {
                        "mode": "pty_retry",
                        "state": "running",
                        "message": f"PTY 传输中断，正在重连续传 {attempt}/{maximum}：{exc}",
                    }
                    time.sleep(min(5, 2 ** failed_attempts))
        self._transfer_status = {
            "mode": "scp",
            "state": "running",
            "message": "正在通过直连 SCP 传输",
        }
        try:
            self._run_direct(
                [*self._scp_base(), str(local_path), f"{self.target}:{remote_path}"], timeout
            )
        except HpcError as exc:
            self._transfer_status = {
                "mode": "scp",
                "state": "failed",
                "message": str(exc).replace("\n", " ")[:240],
            }
            raise
        if progress:
            size = local_path.stat().st_size
            progress(size, size)
        self._transfer_status = {
            "mode": "scp",
            "state": "succeeded",
            "message": "直连 SCP 传输成功",
        }

    def _upload_text_atomic(self, content: str, remote_path: str) -> None:
        data = content.encode("utf-8")
        temporary_path = remote_path + ".part"
        if self._uses_session_transport():
            with self._session_lock:
                self._upload_bytes_session_locked(
                    BytesIO(data),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    temporary_path,
                    120,
                )
                self._run_session_locked(
                    f"mv {self._quote_remote(temporary_path)} {self._quote_remote(remote_path)}", 60
                )
            return
        with tempfile.NamedTemporaryFile("wb", delete=False) as output:
            output.write(data)
            local_path = Path(output.name)
        try:
            self.upload(local_path, temporary_path, timeout=120)
            self.run(
                f"mv {self._quote_remote(temporary_path)} {self._quote_remote(remote_path)}",
                timeout=60,
            )
        finally:
            local_path.unlink(missing_ok=True)

    def _download_file_session(
        self,
        remote_path: str,
        local_path: Path,
        timeout: int,
        *,
        expected_meta: tuple[int, str] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        remote_meta = expected_meta or self._remote_meta(remote_path, timeout=120)
        if remote_meta is None:
            raise HpcError(f"tx-lab 输出不存在：{posixpath.basename(remote_path)}")
        expected_size, expected_digest = remote_meta
        if local_path.is_file() and local_path.stat().st_size == expected_size:
            if _sha256(local_path) == expected_digest:
                if progress:
                    progress(expected_size, expected_size)
                return
        chunk_size = self.settings.hpc_download_chunk_mb * MEBIBYTE
        part = local_path.with_name(local_path.name + ".part")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        aligned = min(offset, expected_size) // chunk_size * chunk_size
        if offset != aligned:
            with part.open("r+b") as output:
                output.truncate(aligned)
        if offset > expected_size:
            with part.open("wb"):
                pass
            aligned = 0
        if progress:
            progress(aligned, expected_size)

        remote = self._quote_remote(remote_path)
        maximum = self.settings.hpc_transfer_retries
        file_attempt = 0
        while True:
            with part.open("ab") as output:
                chunk_index = output.tell() // chunk_size
                while output.tell() < expected_size:
                    expected_chunk_size = min(
                        chunk_size,
                        expected_size - output.tell(),
                    )
                    for failed_attempts in range(maximum + 1):
                        try:
                            encoded = self.run(
                                f"dd if={remote} bs={chunk_size} skip={chunk_index} count=1 status=none "
                                "| base64 | tr -d '\\n'",
                                timeout=timeout,
                            )
                            data = base64.b64decode(encoded, validate=True)
                            if len(data) != expected_chunk_size:
                                raise HpcError(
                                    f"分块长度不一致：期望 {expected_chunk_size}，实际 {len(data)}"
                                )
                            break
                        except (HpcError, ValueError, binascii.Error) as exc:
                            self.close_session()
                            if failed_attempts >= maximum:
                                name = posixpath.basename(remote_path)
                                raise HpcError(
                                    f"超算输出分块下载重试耗尽：{name}，块 {chunk_index}：{exc}"
                                ) from exc
                            time.sleep(min(5, 2 ** failed_attempts))
                    output.write(data)
                    output.flush()
                    chunk_index += 1
                    if progress:
                        progress(output.tell(), expected_size)
            if part.stat().st_size == expected_size and _sha256(part) == expected_digest:
                break
            with part.open("wb"):
                pass
            if progress:
                progress(0, expected_size)
            self.close_session()
            if file_attempt >= maximum:
                raise HpcError("超算输出下载后大小或 SHA256 校验失败，完整文件重试耗尽")
            file_attempt += 1
            time.sleep(min(5, 2 ** (file_attempt - 1)))

        if part.stat().st_size != expected_size or _sha256(part) != expected_digest:
            with part.open("wb"):
                pass
            raise HpcError("超算输出下载后大小或 SHA256 校验失败")
        os.replace(part, local_path)

    def _remote_glob_inventory(
        self,
        remote_dir: str,
        pattern: str,
    ) -> list[tuple[str, int, str]]:
        output = self.run(
            f"cd {self._quote_remote(remote_dir)} || exit 1; "
            f"find . -maxdepth 1 -type f -name {shlex.quote(pattern)} -printf '%f\\n' | sort | "
            "while IFS= read -r name; do "
            "size=$(wc -c < \"$name\") || exit 1; "
            "sha=$(sha256sum \"$name\" | awk '{print $1}') || exit 1; "
            "printf 'FILE:%s SIZE:%s SHA:%s\\n' \"$name\" \"$size\" \"$sha\"; "
            "done",
            timeout=300,
        )
        inventory: list[tuple[str, int, str]] = []
        seen: set[str] = set()
        for line in output.splitlines():
            match = re.fullmatch(
                r"FILE:([^ ]+) SIZE:([0-9]+) SHA:([0-9a-fA-F]{64})",
                line.strip(),
            )
            if not match:
                raise HpcError("超算 wrfout 清单格式异常")
            name, size, digest = match.groups()
            if not SAFE_OUTPUT_NAME.fullmatch(name) or name in seen:
                raise HpcError("超算返回了不安全或重复的输出文件名")
            seen.add(name)
            inventory.append((name, int(size), digest.lower()))
        if not inventory:
            raise HpcError("超算任务成功但没有找到 wrfout 输出")
        return inventory

    def download_glob(
        self,
        remote_glob: str,
        local_dir: Path,
        timeout: int = 1800,
        progress: Callable[[int, int], None] | None = None,
        include_names: set[str] | None = None,
    ) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        if not self._uses_session_transport() and include_names is None:
            self._run_direct(
                [*self._scp_base(), f"{self.target}:{remote_glob}", str(local_dir)], timeout
            )
            if progress:
                progress(1, 1)
            return
        remote_dir, pattern = posixpath.split(remote_glob)
        if not SAFE_REMOTE_ROOT.fullmatch(remote_dir) or not SAFE_OUTPUT_NAME.fullmatch(pattern):
            raise ValueError("非法的远端输出匹配路径")
        inventory = self._remote_glob_inventory(remote_dir, pattern)
        if include_names is not None:
            if any(not SAFE_OUTPUT_NAME.fullmatch(name) for name in include_names):
                raise ValueError("包含非法的远端输出文件名")
            inventory = [item for item in inventory if item[0] in include_names]
            if not inventory:
                raise HpcError("tx-lab 任务没有可下载的有效 wrfout 输出")
        total_size = sum(size for _name, size, _digest in inventory)
        completed_size = 0
        if progress:
            progress(0, total_size)
        for name, size, digest in inventory:
            base_size = completed_size

            def file_progress(done: int, _total: int, *, base: int = base_size) -> None:
                if progress:
                    progress(min(total_size, base + done), total_size)

            self._download_file_session(
                f"{remote_dir}/{name}",
                local_dir / _safe_local_output_name(name),
                timeout,
                expected_meta=(size, digest),
                progress=file_progress,
            )
            completed_size += size

    def download_downscale_bundle(
        self,
        task_id: str,
        local_path: Path,
        timeout: int = 1800,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Archive and download the validated remote CFD product bundle."""
        remote_product = f"{self.downscale_output_dir(task_id)}/product"
        remote_archive = self.downscale_bundle_path(task_id)
        self.run(
            f"test -s {self._quote_remote(remote_product)}/downscale.product.json || exit 1; "
            f"rm -f {self._quote_remote(remote_archive)}.part; "
            f"tar -czf {self._quote_remote(remote_archive)}.part -C {self._quote_remote(remote_product)} . && "
            f"mv {self._quote_remote(remote_archive)}.part {self._quote_remote(remote_archive)}",
            timeout=300,
        )
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if self._uses_session_transport():
            self._download_file_session(remote_archive, local_path, timeout, progress=progress)
        else:
            self._run_direct([*self._scp_base(), f"{self.target}:{remote_archive}", str(local_path)], timeout)
            if progress:
                size = local_path.stat().st_size
                progress(size, size)
        return local_path

    def health(self) -> dict[str, Any]:
        try:
            gfs_dir = self._quote_remote(self.settings.hpc_gfs_dir)
            gfs_mount = self._quote_remote(self.settings.hpc_gfs_mount)
            output = self.run(
                f"missing=''; test -x {self.settings.hpc_wps_source_dir}/geogrid.exe || missing=\"$missing WPS/geogrid\"; "
                f"test -x {self.settings.hpc_wps_source_dir}/ungrib.exe || missing=\"$missing WPS/ungrib\"; "
                f"test -x {self.settings.hpc_wps_source_dir}/metgrid.exe || missing=\"$missing WPS/metgrid\"; "
                f"test -x {self.settings.hpc_wrf_source_dir}/main/real.exe || missing=\"$missing WRF-CPU/real\"; "
                f"test -x {self.settings.hpc_wrf_source_dir}/main/wrf.exe || missing=\"$missing WRF-CPU/wrf\"; "
                f"test -d {self.settings.hpc_geog_dir} || missing=\"$missing WPS_GEOG\"; "
                f"test -r {self.settings.hpc_runtime_env} || missing=\"$missing ENV\"; "
                f"findmnt -rn -T {gfs_mount} >/dev/null 2>&1 || missing=\"$missing GFS_MOUNT\"; "
                f"if ! mkdir -p {gfs_dir} 2>/dev/null; then missing=\"$missing GFS_IO\"; "
                f"elif probe=$(mktemp {gfs_dir}/.health-probe.XXXXXX 2>/dev/null); then rm -f \"$probe\"; "
                "else missing=\"$missing GFS_IO\"; fi; "
                "hard_missing=\"$missing\"; space_missing=''; "
                f"gfs_free=$(df -Pk {gfs_dir} 2>/dev/null | awk 'NR==2{{print int($4/1024/1024)}}'); "
                f"runtime_free=$(df -Pk {self._quote_remote(self.settings.hpc_remote_dir)} 2>/dev/null | awk 'NR==2{{print int($4/1024/1024)}}'); "
                f"test \"${{gfs_free:-0}}\" -ge {self.settings.hpc_gfs_min_free_gb} 2>/dev/null || {{ missing=\"$missing GFS_SPACE\"; space_missing=1; }}; "
                f"test \"${{runtime_free:-0}}\" -ge {self.settings.hpc_runtime_min_free_gb} 2>/dev/null || {{ missing=\"$missing RUNTIME_SPACE\"; space_missing=1; }}; "
                "if test -z \"$missing\"; then printf 'WRF_HPC_READY|gfs_free_gb=%s|runtime_free_gb=%s' \"$gfs_free\" \"$runtime_free\"; "
                "elif test -z \"$hard_missing\" && test -n \"$space_missing\"; then "
                "printf 'WRF_HPC_STORAGE_LOW|missing=%s|gfs_free_gb=%s|runtime_free_gb=%s' \"$missing\" \"$gfs_free\" \"$runtime_free\"; "
                "else printf 'WRF_HPC_MISSING|missing=%s|gfs_free_gb=%s|runtime_free_gb=%s' \"$missing\" \"$gfs_free\" \"$runtime_free\"; fi",
                timeout=self.settings.hpc_connect_timeout + 10,
            )
            ready = "WRF_HPC_READY" in output
            storage_unavailable = "GFS_MOUNT" in output or "GFS_IO" in output
            metrics: dict[str, int] = {}
            for key, value in re.findall(r"(gfs_free_gb|runtime_free_gb)=([0-9]+)", output):
                metrics[key] = int(value)
            storage_low = "WRF_HPC_STORAGE_LOW" in output
            if ready:
                message = "tx-lab 运行环境与数据池空间正常"
            elif storage_low:
                message = (
                    f"空间不足：GFS 可用 {metrics.get('gfs_free_gb', 0)} GiB（需要 ≥{self.settings.hpc_gfs_min_free_gb} GiB），"
                    f"运行目录可用 {metrics.get('runtime_free_gb', 0)} GiB（需要 ≥{self.settings.hpc_runtime_min_free_gb} GiB）；"
                    "已暂停后台预取，请清理旧数据或扩容后重试"
                )
            else:
                message = output
            return {
                "status": "ready" if ready else ("storage_unavailable" if storage_unavailable else ("storage_low" if storage_low else "unavailable")),
                "message": message,
                "raw_message": output,
                "target": f"{self.target}:{self.settings.hpc_port}",
                "connection_mode": self.settings.hpc_connection_mode,
                "transfer": self.transfer_status,
                "gfs_scope": GFS_SCOPE,
                "gfs_bounds": self.gfs_bounds,
                "gfs_storage_path": self.settings.hpc_gfs_dir,
                **metrics,
                **self.connection_diagnostic,
            }
        except HpcStorageError as exc:
            return {
                "status": "storage_unavailable",
                "message": str(exc),
                "target": f"{self.target}:{self.settings.hpc_port}",
                "connection_mode": self.settings.hpc_connection_mode,
                "transfer": self.transfer_status,
                **self.connection_diagnostic,
            }
        except HpcAuthError:
            return {
                "status": "auth_required",
                "message": "tx-lab SSH 密钥未就绪",
                "target": f"{self.target}:{self.settings.hpc_port}",
                "connection_mode": self.settings.hpc_connection_mode,
                "transfer": self.transfer_status,
                **self.connection_diagnostic,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "message": str(exc),
                "target": f"{self.target}:{self.settings.hpc_port}",
                "connection_mode": self.settings.hpc_connection_mode,
                "transfer": self.transfer_status,
                **self.connection_diagnostic,
            }

    def _assert_task_id(self, task_id: str) -> None:
        if not SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("非法任务 ID")

    @staticmethod
    def _assert_cycle(cycle: str) -> None:
        if not SAFE_CYCLE.fullmatch(cycle):
            raise ValueError("非法 GFS cycle")

    def service_dir(self) -> str:
        return f"{self.settings.hpc_remote_dir}/backend_wrf_service"

    def task_dir(self, task_id: str) -> str:
        self._assert_task_id(task_id)
        return f"{self.settings.hpc_remote_dir}/backend_wrf_tasks/{task_id}"

    def remote_output_dir(self, task_id: str) -> str:
        self._assert_task_id(task_id)
        return f"{self.settings.hpc_remote_dir}/WRF_{task_id}/run"

    def inspect_wrf_source(self, task_id: str) -> dict[str, Any]:
        """Inventory stable native WRF files used by the high-resolution hand-off."""
        output_dir = self.remote_output_dir(task_id)
        quoted_dir = self._quote_remote(output_dir)
        command = (
            f"if test ! -d {quoted_dir}; then printf 'MISSING_DIR'; exit 0; fi; "
            f"names=$(find -L {quoted_dir} -maxdepth 1 -type f "
            "\\( -name 'wrfout_d04_*' -o -name 'wrfrst_d04_*' \\) -printf '%f\\n' | sort); "
            "before=$(mktemp /tmp/wrf-source-inventory.XXXXXX); "
            "trap 'rm -f \"$before\"' EXIT; "
            "printf '%s\\n' \"$names\" | while IFS= read -r name; do "
            "test -n \"$name\" || continue; "
            f"printf '%s|%s\\n' \"$name\" \"$(wc -c < {quoted_dir}/\"$name\" 2>/dev/null | tr -d '[:space:]')\"; done > \"$before\"; "
            "sleep 2; "
            "while IFS='|' read -r name size1; do "
            "test -n \"$name\" || continue; "
            f"size2=$(wc -c < {quoted_dir}/\"$name\" 2>/dev/null | tr -d '[:space:]'); "
            f"mtime=$(stat -c '%Y' {quoted_dir}/\"$name\" 2>/dev/null || printf 0); "
            f"sha=$(sha256sum {quoted_dir}/\"$name\" 2>/dev/null | awk '{{print $1}}'); "
            "case \"$name\" in wrfout_*) kind=history;; wrfrst_*) kind=restart;; *) continue;; esac; "
            "printf '%s|%s|%s|%s|%s|%s\\n' \"$kind\" \"$name\" \"$size1\" \"$size2\" \"$mtime\" \"$sha\"; done < \"$before\""
        )
        raw = self.run(command, timeout=900)
        if raw.strip() == "MISSING_DIR":
            return {"remote_output_dir": output_dir, "files": [], "inventory_sha256": ""}
        files: list[dict[str, Any]] = []
        for line in raw.splitlines():
            kind, separator, remainder = line.partition("|")
            if not separator:
                continue
            name, separator, remainder = remainder.partition("|")
            if not separator or not SAFE_OUTPUT_NAME.fullmatch(name):
                continue
            size_text, separator, remainder = remainder.partition("|")
            if not separator:
                continue
            size2_text, separator, remainder = remainder.partition("|")
            if not separator:
                continue
            mtime_text, separator, digest = remainder.partition("|")
            if not separator:
                continue
            try:
                size1, size2, mtime = int(size_text), int(size2_text), int(mtime_text)
            except ValueError:
                continue
            files.append({
                "kind": kind,
                "name": name,
                "size_bytes": size2,
                "mtime_utc": datetime.fromtimestamp(mtime, timezone.utc).isoformat().replace("+00:00", "Z") if mtime > 0 else "",
                "sha256": digest.lower(),
                "stable": size1 == size2 and size2 > 0,
            })
        inventory_payload = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return {
            "remote_output_dir": output_dir,
            "files": files,
            "inventory_sha256": hashlib.sha256(inventory_payload).hexdigest(),
        }

    def _assert_downscale_task_id(self, task_id: str) -> None:
        if not SAFE_DOWNSCALE_TASK_ID.fullmatch(task_id):
            raise ValueError("非法高分辨率任务 ID")

    def downscale_task_dir(self, task_id: str) -> str:
        self._assert_downscale_task_id(task_id)
        return f"{self.settings.hpc_remote_dir}/backend_wrf_downscale_tasks/{task_id}"

    def downscale_output_dir(self, task_id: str) -> str:
        self._assert_downscale_task_id(task_id)
        # High-resolution native outputs are run assets. Keep them below the
        # UUID-resolved data volume instead of the RUNTIME control directory,
        # so a reboot or a changed mount label cannot silently fill /.
        return f"{self.settings.hpc_storage_alias}/WRFwork/DOWNSCALE_{task_id}/run"

    def downscale_bundle_path(self, task_id: str) -> str:
        return f"{self.downscale_output_dir(task_id)}/downscale.product.bundle.tar.gz"

    def prepare_downscale_runtime(
        self, task_id: str, contract_path: Path, script_dir: Path,
        surface_assets: list[tuple[Path, str]] | None = None,
    ) -> None:
        task_dir = self.downscale_task_dir(task_id)
        service_dir = self.service_dir()
        self.run(f"mkdir -p {self._quote_remote(task_dir)} {self._quote_remote(service_dir)}")
        items = [
            (script_dir / "wrf_hpc_downscale.sh", f"{service_dir}/wrf_hpc_downscale.sh"),
            (script_dir / "wrf_hpc_downscale_chain.sh", f"{service_dir}/wrf_hpc_downscale_chain.sh"),
            (script_dir / "wrf_hpc_downscale_chain.py", f"{service_dir}/wrf_hpc_downscale_chain.py"),
            (script_dir / "wrf_hpc_cfd_10m_chain.py", f"{service_dir}/wrf_hpc_cfd_10m_chain.py"),
            (script_dir / "wrf_hpc_cfd_chain.py", f"{service_dir}/wrf_hpc_cfd_chain.py"),
            (script_dir / "wrf_hpc_cfd_case.py", f"{service_dir}/wrf_hpc_cfd_case.py"),
            (script_dir / "wrf_hpc_cfd_products.py", f"{service_dir}/wrf_hpc_cfd_products.py"),
            (script_dir / "cfd_support" / "build_wrf_cfd_interface.py", f"{service_dir}/build_wrf_cfd_interface.py"),
            (script_dir / "cfd_support" / "apply_profile_boundaries.py", f"{service_dir}/apply_profile_boundaries.py"),
            (script_dir / "wrf_storage_guard.sh", f"{service_dir}/wrf_storage_guard.sh"),
            (contract_path, f"{task_dir}/downscale.job.json"),
        ]
        for local_path, remote_path in items:
            self.upload(local_path, remote_path, timeout=600)
        if surface_assets:
            surface_dir = f"{task_dir}/surface"
            self.run(f"mkdir -p {self._quote_remote(surface_dir)}; chmod 700 {self._quote_remote(surface_dir)}")
            for local_path, safe_name in surface_assets:
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", safe_name):
                    raise ValueError("非法地形资产文件名")
                self.upload(local_path, f"{surface_dir}/{safe_name}", timeout=1800)
        self.run(
            f"chmod 700 {self._quote_remote(service_dir)}/wrf_hpc_downscale.sh "
            f"{self._quote_remote(service_dir)}/wrf_hpc_downscale_chain.sh "
            f"{self._quote_remote(service_dir)}/wrf_hpc_cfd_10m_chain.py "
            f"{self._quote_remote(service_dir)}/wrf_hpc_cfd_chain.py "
            f"{self._quote_remote(service_dir)}/wrf_hpc_cfd_case.py "
            f"{self._quote_remote(service_dir)}/wrf_hpc_cfd_products.py "
            f"{self._quote_remote(service_dir)}/wrf_storage_guard.sh; "
            f"chmod 600 {self._quote_remote(task_dir)}/downscale.job.json"
        )
        self.run(
            f"python3 -m py_compile {self._quote_remote(service_dir)}/wrf_hpc_downscale_chain.py && "
            f"{self._quote_remote(self.settings.hpc_downscale_python)} -m py_compile "
            f"{self._quote_remote(service_dir)}/wrf_hpc_cfd_10m_chain.py "
            f"{self._quote_remote(service_dir)}/wrf_hpc_cfd_case.py "
            f"{self._quote_remote(service_dir)}/wrf_hpc_cfd_products.py",
            timeout=120,
        )

    def launch_downscale(self, task_id: str) -> dict[str, Any]:
        task_dir = self.downscale_task_dir(task_id)
        service_dir = self.service_dir()
        output_dir = self.downscale_output_dir(task_id)
        # A retry reuses the isolated task directory and its completed stage
        # markers, but process-control files belong to one launch attempt.
        # Remove them before preflight so status polling cannot observe a
        # previous non-zero exit while the new runner is already active.
        self.run(
            f"rm -f {self._quote_remote(task_dir)}/service.pid {self._quote_remote(task_dir)}/exit.code"
        )
        runtime = (
            f"WORK_DIR={self._quote_remote(self.settings.hpc_remote_dir)} "
            f"WRF_CPU_SOURCE_DIR={self._quote_remote(self.settings.hpc_wrf_source_dir)} "
            f"WRF_RUNTIME_ENV={self._quote_remote(self.settings.hpc_runtime_env)} "
            f"WRF_DOWNSCALE_CONTRACT={self._quote_remote(task_dir)}/downscale.job.json "
            f"WRF_DOWNSCALE_TASK_DIR={self._quote_remote(task_dir)} "
            f"WRF_DOWNSCALE_SURFACE_DIR={self._quote_remote(task_dir)}/surface "
            f"WRF_DOWNSCALE_OUTPUT_DIR={self._quote_remote(output_dir)} "
            f"WRF_DOWNSCALE_CFD_SCRIPT={self._quote_remote(service_dir)}/wrf_hpc_cfd_chain.py "
            f"WRF_DOWNSCALE_CFD_10M_SCRIPT={self._quote_remote(service_dir)}/wrf_hpc_cfd_10m_chain.py "
            f"WRF_DOWNSCALE_CFD_SCRIPT_DIR={self._quote_remote(service_dir)} "
            f"WRF_DOWNSCALE_INTERFACE_ROOT={self._quote_remote(self.settings.hpc_downscale_interface_root)} "
            f"WRF_DOWNSCALE_CFD_TEMPLATE={self._quote_remote(self.settings.hpc_downscale_cfd_template)} "
            f"WRF_DOWNSCALE_OPENFOAM_ACTIVATE={self._quote_remote(self.settings.hpc_downscale_openfoam_activate)} "
            f"WRF_DOWNSCALE_PYTHON={self._quote_remote(self.settings.hpc_downscale_python)} "
            f"WRF_STORAGE_GUARD_SCRIPT={self._quote_remote(service_dir)}/wrf_storage_guard.sh "
            f"WRF_STORAGE_ALIAS={self._quote_remote(self.settings.hpc_storage_alias)} "
            f"WRF_STORAGE_UUID={self._quote_remote(self.settings.hpc_storage_uuid)} "
            f"WRF_NDOWN_EXE={self._quote_remote(self.settings.hpc_wrf_source_dir)}/main/ndown.exe "
            "WRF_DOWNSCALE_RUN_CHAIN=true "
            "WRF_NONINTERACTIVE=true "
        )
        preflight = self.run(
            f"cd {self._quote_remote(task_dir)} || exit 1; {runtime}WRF_DOWNSCALE_PREFLIGHT_ONLY=true "
            f"bash {self._quote_remote(service_dir)}/wrf_hpc_downscale.sh",
            timeout=300,
        )
        runner = (
            f"printf '%s\\n' $$ > {self._quote_remote(task_dir)}/service.pid; "
            f"{runtime}bash {self._quote_remote(service_dir)}/wrf_hpc_downscale.sh; "
            f"code=$?; printf '%s\\n' \"$code\" > {self._quote_remote(task_dir)}/exit.code; exit \"$code\""
        )
        command = (
            f"cd {self._quote_remote(task_dir)} || exit 1; "
            f"nohup setsid bash -c {shlex.quote(runner)} "
            f"> {self._quote_remote(task_dir)}/service.log 2>&1 < /dev/null & "
            f"for attempt in 1 2 3 4 5 6 7 8 9 10; do "
            f"test -s {self._quote_remote(task_dir)}/service.pid && break; sleep 0.1; done; "
            f"cat {self._quote_remote(task_dir)}/service.pid"
        )
        pid = self.run(command).splitlines()[-1]
        if not pid.isdigit():
            raise HpcError("未获得远端高分辨率进程 ID")
        return {"remote_pid": int(pid), "remote_task_dir": task_dir, "remote_output_dir": output_dir, "preflight": preflight.strip()}

    def downscale_status(self, task_id: str) -> dict[str, Any]:
        task_dir = self.downscale_task_dir(task_id)
        quoted = self._quote_remote(task_dir)
        state = self.run(
            f"if test -f {quoted}/exit.code; then printf 'EXIT:'; cat {quoted}/exit.code; "
            f"elif test -f {quoted}/service.pid && kill -0 \"$(cat {quoted}/service.pid)\" 2>/dev/null; then printf RUNNING; "
            f"elif test -f {quoted}/service.pid; then printf LOST; else printf MISSING; fi"
        ).strip()
        log = self.run(f"tail -n 80 {quoted}/service.log 2>/dev/null || true", timeout=60)
        if state.startswith("EXIT:0"):
            return {"status": "succeeded", "log": log}
        if state.startswith("EXIT:"):
            return {"status": "failed", "exit_code": state.split(":", 1)[1].strip(), "log": log}
        if state == "RUNNING":
            return {"status": "running", "log": log}
        return {"status": state.lower(), "log": log}

    def inspect_downscale_forcing_manifest(self, output_dir: str) -> dict[str, Any] | None:
        """Read one completed d08 manifest without downloading native forcing."""
        expected_prefix = f"{self.settings.hpc_storage_alias.rstrip('/')}/WRFwork/DOWNSCALE_"
        if not output_dir.startswith(expected_prefix) or not output_dir.endswith("/run"):
            raise ValueError("约 10 m forcing 输出目录不在受管数据盘")
        manifest_path = f"{output_dir}/mesoscale_forcing_manifest.json"
        raw = self.run(f"cat {self._quote_remote(manifest_path)}", timeout=60)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("约 10 m forcing manifest 不是 JSON 对象")
        return value

    def task_artifact_paths(self, task_id: str) -> list[str]:
        """返回同一任务尝试可清理的精确受管路径，不包含共享 GFS 数据池。"""
        self._assert_task_id(task_id)
        root = self._absolute_remote_path(self.settings.hpc_remote_dir).rstrip("/")
        return [
            posixpath.join(root, "backend_wrf_tasks", task_id),
            posixpath.join(root, f"WPS_{task_id}_attempt-1-cpu"),
            posixpath.join(root, f"WPS_{task_id}_attempt-1-gpu"),
            posixpath.join(root, f"WPS_{task_id}_attempt-2-cpu"),
            posixpath.join(root, f"WRF_{task_id}"),
            posixpath.join(root, f"WRF_{task_id}_attempt-1-cpu"),
            posixpath.join(root, f"WRF_{task_id}_attempt-1-gpu"),
            posixpath.join(root, f"WRF_{task_id}_attempt-2-cpu"),
            posixpath.join(root, f"logs_{task_id}_attempt-1-cpu"),
            posixpath.join(root, f"logs_{task_id}_attempt-1-gpu"),
            posixpath.join(root, f"logs_{task_id}_attempt-2-cpu"),
            posixpath.join(root, f"stage_status_{task_id}_attempt-1-cpu.jsonl"),
            posixpath.join(root, f"stage_status_{task_id}_attempt-1-gpu.jsonl"),
            posixpath.join(root, f"stage_status_{task_id}_attempt-2-cpu.jsonl"),
        ]

    def prepare_runtime(
        self,
        task_id: str,
        config_path: Path,
        environment_path: Path,
        expected_forcing_path: Path,
        script_dir: Path,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        service_dir, task_dir = self.service_dir(), self.task_dir(task_id)
        self.run(f"mkdir -p {service_dir} {task_dir}")
        items = [
            (script_dir / "wrf.sh", f"{service_dir}/wrf.sh"),
            (script_dir / "wrf_hpc_gfs.sh", f"{service_dir}/wrf_hpc_gfs.sh"),
            (script_dir / "wrf_storage_guard.sh", f"{service_dir}/wrf_storage_guard.sh"),
            (script_dir / "download_gfs_00z.sh", f"{service_dir}/download_gfs_00z.sh"),
            (script_dir / "download_ecmwf_00z.sh", f"{service_dir}/download_ecmwf_00z.sh"),
            (script_dir / "ecmwf_to_wps.filter", f"{service_dir}/ecmwf_to_wps.filter"),
            (config_path, f"{task_dir}/task.json"),
            (environment_path, f"{task_dir}/task.env"),
            (expected_forcing_path, f"{task_dir}/forcing.expected.tsv"),
        ]
        total = sum(path.stat().st_size for path, _remote in items)
        completed = 0
        for path, remote in items:
            def item_progress(done: int, _size: int, *, name: str = path.name, base: int = completed) -> None:
                if progress:
                    progress(name, base + done, total)

            self.upload(path, remote, progress=item_progress)
            completed += path.stat().st_size
        self.run(
            f"chmod 700 {service_dir}/wrf.sh {service_dir}/wrf_hpc_gfs.sh {service_dir}/wrf_storage_guard.sh {service_dir}/download_gfs_00z.sh {service_dir}/download_ecmwf_00z.sh; "
            f"chmod 600 {task_dir}/task.json {task_dir}/task.env {task_dir}/forcing.expected.tsv"
        )

    @staticmethod
    def _manifest_entry(item: Any, cycle: str) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "")
        match = SAFE_GFS_NAME.fullmatch(name)
        digest = str(item.get("sha256") or "").lower()
        try:
            hour = int(item.get("forecast_hour"))
            size = int(item.get("size"))
        except (TypeError, ValueError):
            return None
        if (
            not match
            or name[5:7] != cycle[8:10]
            or int(match.group(1)) != hour
            or size <= 0
            or not SHA256.fullmatch(digest)
        ):
            return None
        return {"name": name, "forecast_hour": hour, "size": size, "sha256": digest}

    def inspect_gfs_files(self, cycle: str, forecast_hours: list[int]) -> dict[str, Any]:
        self._assert_cycle(cycle)
        requested = sorted(set(int(hour) for hour in forecast_hours))
        remote_dir = f"{self.settings.hpc_gfs_dir}/{cycle}"
        manifest_path = f"{remote_dir}/manifest.json"
        raw = self.run(
            f"if test -s {manifest_path}; then cat {manifest_path}; else printf '{{}}'; fi",
            timeout=60,
        )
        try:
            manifest = json.loads(raw or "{}")
        except json.JSONDecodeError:
            manifest = {}
        candidates: dict[str, dict[str, Any]] = {}
        managed_manifest = (
            isinstance(manifest, dict)
            and manifest.get("cycle") == cycle
            and manifest.get("product") == GFS_PRODUCT
            and manifest.get("scope") == GFS_SCOPE
            and manifest.get("bounds") == self.gfs_bounds
        )
        if managed_manifest:
            for item in manifest.get("files", []):
                entry = self._manifest_entry(item, cycle)
                if entry is not None:
                    candidates[entry["name"]] = entry
        queue_files = ((self._pool_queue_state(self.settings.hpc_gfs_dir).get("cycles") or {}).get(cycle) or {}).get("files") or {}

        expected = {
            f"gfs.t{cycle[8:10]}z.pgrb2.0p25.f{hour:03d}": hour
            for hour in requested
        }
        actual_sizes: dict[str, int] = {}
        if expected:
            names = " ".join(shlex.quote(name) for name in sorted(expected))
            command = (
                f"cd {self._quote_remote(remote_dir)} 2>/dev/null || exit 0; for name in {names}; do "
                "if test -f \"$name\" && test \"$(head -c 4 \"$name\" 2>/dev/null)\" = GRIB "
                "&& test \"$(tail -c 4 \"$name\" 2>/dev/null)\" = 7777; then "
                "size=$(wc -c < \"$name\"); "
                "printf '%s|%s\\n' \"$name\" \"$size\"; fi; done"
            )
            for line in self.run(command, timeout=300).splitlines():
                parts = line.split("|")
                if len(parts) != 2 or parts[0] not in expected:
                    continue
                try:
                    size = int(parts[1])
                except ValueError:
                    continue
                if size >= self.settings.hpc_gfs_full_min_bytes:
                    actual_sizes[parts[0]] = size

        valid_entries: list[dict[str, Any]] = []
        unhashed: list[str] = []
        for name, hour in expected.items():
            size = actual_sizes.get(name)
            if size is None:
                continue
            candidate = candidates.get(name)
            if candidate and size == candidate["size"]:
                valid_entries.append(candidate)
            elif isinstance(queue_files.get(f"{hour:03d}"), dict):
                queue_entry = queue_files[f"{hour:03d}"]
                digest = str(queue_entry.get("sha256") or "").lower()
                if queue_entry.get("status") == "ready" and int(queue_entry.get("bytes") or 0) == size and SHA256.fullmatch(digest):
                    valid_entries.append({"name": name, "forecast_hour": hour, "size": size, "sha256": digest})
                else:
                    unhashed.append(name)
            else:
                unhashed.append(name)
        if unhashed:
            names = " ".join(shlex.quote(name) for name in sorted(unhashed))
            command = (
                f"cd {self._quote_remote(remote_dir)} || exit 1; for name in {names}; do "
                "sha=$(sha256sum \"$name\" | awk '{print $1}'); "
                "printf '%s|%s\\n' \"$name\" \"$sha\"; done"
            )
            for line in self.run(command, timeout=1800).splitlines():
                name, separator, digest = line.partition("|")
                if not separator or name not in expected or not SHA256.fullmatch(digest.lower()):
                    continue
                valid_entries.append(
                    {
                        "name": name,
                        "forecast_hour": expected[name],
                        "size": actual_sizes[name],
                        "sha256": digest.lower(),
                    }
                )

        valid_by_hour = {entry["forecast_hour"]: entry for entry in valid_entries}
        valid_hours = [hour for hour in requested if hour in valid_by_hour]
        missing_hours = [hour for hour in requested if hour not in valid_by_hour]
        return {
            "complete": not missing_hours,
            "requested_hours": requested,
            "valid_hours": valid_hours,
            "missing_hours": missing_hours,
            "entries": sorted(valid_entries, key=lambda item: item["forecast_hour"]),
            "remote_dir": remote_dir,
            "scope": GFS_SCOPE,
            "bounds": self.gfs_bounds,
            "legacy_imported_hours": [],
            "manifest_needs_rebuild": bool(unhashed),
            "manifest_is_full": managed_manifest,
            "manifest_is_regional": managed_manifest,
        }

    def trigger_gfs_download(
        self,
        cycle: str,
        horizon: int = 72,
        forecast_hours: list[int] | None = None,
    ) -> dict[str, Any]:
        """Enqueue a shared GFS download without waiting for the pool worker."""
        self._assert_cycle(cycle)
        if cycle[8:10] != "00":
            raise ValueError("当前 WRF 数据池仅允许触发 00Z 周期")
        horizon = max(0, min(72, int(horizon)))
        if forecast_hours is None:
            requested_hours = list(range(horizon + 1))
        else:
            requested_hours = sorted({int(hour) for hour in forecast_hours})
            if not requested_hours or requested_hours[0] < 0 or requested_hours[-1] > 72:
                raise ValueError("GFS 下载时次必须位于 f000-f072")
        requested_spec = ",".join(str(hour) for hour in requested_hours)
        intent = "task" if forecast_hours is not None else "prefetch"
        local_script = Path(__file__).resolve().parent / "scripts" / "download_gfs_00z.sh"
        local_queue_script = Path(__file__).resolve().parent / "scripts" / "forcing_pool_queue.py"
        remote_parent = posixpath.dirname(self.settings.hpc_gfs_download_script)
        self.run(f"mkdir -p {self._quote_remote(remote_parent)}", timeout=60)
        self.upload(local_queue_script, f"{remote_parent}/forcing_pool_queue.py", timeout=300)
        self.upload(local_script, self.settings.hpc_gfs_download_script, timeout=300)
        self.run(
            f"chmod 700 {self._quote_remote(self.settings.hpc_gfs_download_script)} "
            f"&& chmod 700 {self._quote_remote(f'{remote_parent}/forcing_pool_queue.py')}",
            timeout=60,
        )
        script = self._quote_remote(self.settings.hpc_gfs_download_script)
        log_dir = f"{self.settings.hpc_gfs_dir}/logs"
        log_path = f"{log_dir}/download_{cycle}.out"
        command = (
            f"if ! findmnt -rn -T {self._quote_remote(self.settings.hpc_gfs_mount)} >/dev/null 2>&1; "
            f"then printf 'ERROR|gfs_mount_unavailable'; "
            f"elif ! test -x {script}; then printf 'ERROR|download_script_not_executable'; "
            f"elif ! mkdir -p {self._quote_remote(self.settings.hpc_gfs_dir)} {self._quote_remote(log_dir)} 2>/dev/null; "
            "then printf 'ERROR|gfs_storage_io_error'; "
            f"elif ! probe=$(mktemp {self._quote_remote(log_dir)}/.start-probe.XXXXXX 2>/dev/null); "
            "then printf 'ERROR|gfs_storage_io_error'; "
            "else rm -f \"$probe\"; "
            f"env WRF_GFS_DATA_ROOT={self._quote_remote(self.settings.hpc_gfs_dir)} "
            f"WRF_GFS_MOUNT={self._quote_remote(self.settings.hpc_gfs_mount)} "
            f"WRF_GFS_MIN_FREE_GB={self.settings.hpc_gfs_min_free_gb} "
            f"WRF_GFS_MIN_BYTES={self.settings.hpc_gfs_full_min_bytes} "
            f"WRF_GFS_MIN_SPEED_BPS={self.settings.hpc_gfs_min_speed_bps} "
            f"WRF_GFS_SLOW_SECONDS={self.settings.hpc_gfs_slow_seconds} "
            f"WRF_GFS_REQUEST_INTERVAL_SECONDS={self.settings.hpc_gfs_request_interval_seconds} "
            f"WRF_POOL_RETRY_MAX_ATTEMPTS={self.settings.hpc_pool_retry_max_attempts} "
            f"WRF_POOL_RETRY_MAX_SECONDS={self.settings.hpc_pool_retry_max_seconds} "
            f"WRF_POOL_PUBLISH_PROBE_SECONDS={self.settings.hpc_pool_publish_probe_seconds} "
            f"WRF_POOL_HISTORY_RETENTION_DAYS={self.settings.hpc_pool_history_retention_days} "
            f"bash {script} {cycle} {horizon} {self._quote_remote(requested_spec)} {intent} "
            f"> {self._quote_remote(log_path)} 2>&1; tail -n 1 {self._quote_remote(log_path)}; fi"
        )
        state, _separator, detail = self.run(command, timeout=60).partition("|")
        if state == "ERROR":
            if detail == "gfs_mount_unavailable":
                raise HpcError(f"tx-lab GFS 所在文件系统未就绪：{self.settings.hpc_gfs_mount}")
            if detail == "gfs_storage_io_error":
                raise HpcError(f"tx-lab GFS 系统盘目录不可写：{self.settings.hpc_gfs_dir}")
            raise HpcError("tx-lab GFS 下载脚本不存在或不可执行")
        if state == "QUEUED":
            return {"cycle": cycle, "status": "queued", "detail": f"{intent}:{detail or requested_spec}"}
        return {"cycle": cycle, "status": state.lower(), "detail": detail}

    def ensure_remote_gfs(
        self,
        cycle: str,
        forecast_hours: list[int],
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        protected_cycles: set[str] | None = None,
    ) -> dict[str, Any]:
        """Wait for task-priority pool files; a healthy queue has no fixed timeout."""
        result = self.inspect_gfs_files(cycle, forecast_hours)
        if result["complete"]:
            return result
        trigger = self.trigger_gfs_download(cycle, 72, forecast_hours=forecast_hours)
        if trigger.get("status") in {"failed", "error"}:
            detail = str(trigger.get("detail") or "download process exited immediately")
            raise HpcError(f"tx-lab GFS {cycle} 下载启动失败：{detail}")
        if progress:
            progress({**result, "download": trigger})
        while True:
            if cancelled and cancelled():
                raise HpcError("任务已取消等待；tx-lab 共享 GFS 下载继续运行")
            time.sleep(self.settings.hpc_gfs_poll_seconds)
            result = self.inspect_gfs_files(cycle, forecast_hours)
            if progress:
                progress({**result, "download": trigger})
            if result["complete"]:
                return result
            failure = self._queue_has_terminal_failure("gfs", cycle, result.get("missing_hours") or forecast_hours)
            if failure:
                raise HpcError(f"tx-lab GFS {cycle} 必需驱动文件下载失败：{failure}")

    @staticmethod
    def _normalize_forcing_source(data_source: str) -> str:
        source = str(data_source or "gfs").strip().lower()
        if source == "ec":
            source = "ecmwf"
        if source not in {"gfs", "ecmwf"}:
            raise ValueError("数据源必须是 gfs 或 ecmwf")
        return source

    def _forcing_settings(self, data_source: str) -> dict[str, Any]:
        source = self._normalize_forcing_source(data_source)
        if source == "gfs":
            return {
                "source": source,
                "label": "GFS 全球 0.25°",
                "root": self.settings.hpc_gfs_dir,
                "script": self.settings.hpc_gfs_download_script,
                "local_script": Path(__file__).resolve().parent / "scripts" / "download_gfs_00z.sh",
                "file_glob": "gfs.t00z.pgrb2.0p25.f???",
                "file_name": lambda hour: f"gfs.t00z.pgrb2.0p25.f{hour:03d}",
                "part_glob": "gfs.t00z.pgrb2.0p25.f???.part*",
                "script_name": "download_gfs_00z.sh",
                "min_bytes": self.settings.hpc_gfs_full_min_bytes,
                "poll_seconds": self.settings.hpc_gfs_poll_seconds,
                "product": GFS_PRODUCT,
                "scope": GFS_SCOPE,
                "hours": tuple(range(73)),
                "total_files": 73,
            }
        return {
            "source": source,
            "label": "ECMWF 0.25°",
            "root": self.settings.hpc_ec_dir,
            "script": self.settings.hpc_ec_download_script,
            "local_script": Path(__file__).resolve().parent / "scripts" / "download_ecmwf_00z.sh",
            "file_glob": "ecmwf.t00z.ifs.0p25.f???.grib2",
            "file_name": lambda hour: f"ecmwf.t00z.ifs.0p25.f{hour:03d}.grib2",
            "part_glob": "ecmwf.t00z.ifs.0p25.f???.grib2.part*",
            "script_name": "download_ecmwf_00z.sh",
            "min_bytes": self.settings.hpc_ec_full_min_bytes,
            "poll_seconds": self.settings.hpc_ec_poll_seconds,
            "product": EC_PRODUCT,
            "scope": EC_SCOPE,
            "hours": tuple(range(0, 73, 3)),
            "total_files": 25,
        }

    def _pool_queue_state(self, root: str) -> dict[str, Any]:
        """Read lightweight remote queue telemetry without scanning GRIB data."""
        raw = self.run(
            f"if test -s {self._quote_remote(f'{root}/.pool-queue.json')}; then "
            f"cat {self._quote_remote(f'{root}/.pool-queue.json')}; else printf '{{}}'; fi",
            timeout=60,
        )
        try:
            state = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return state if isinstance(state, dict) and isinstance(state.get("cycles"), dict) else {}

    @staticmethod
    def _queue_cycle_details(state: dict[str, Any], cycle: str) -> dict[str, Any]:
        item = (state.get("cycles") or {}).get(cycle)
        if not isinstance(item, dict):
            return {}
        files = item.get("files") or {}
        entries = [
            entry for entry in files.values()
            if isinstance(entry, dict) and entry.get("status") != "obsolete"
        ]
        statuses = {str(entry.get("status") or "") for entry in entries}
        now = int(time.time())
        failed_entries = [
            entry for entry in entries if entry.get("status") in {"failed", "retry_wait"}
        ]
        retry_wait_entries = [
            entry for entry in failed_entries
            if entry.get("status") == "retry_wait"
            and bool(entry.get("retryable"))
            and int(entry.get("terminal_retry_at") or 0) > now
        ]
        pending_statuses = statuses - {"ready", "failed", "retry_wait"}
        if "downloading" in statuses:
            queue_status = "downloading"
        elif pending_statuses - {"rate_limited", "waiting_publish"}:
            queue_status = "downloading"
        elif "rate_limited" in statuses:
            queue_status = "rate_limited"
        elif str(item.get("publication") or "") in {"waiting_publish", "probing"}:
            queue_status = "waiting_publish"
        elif failed_entries and len(retry_wait_entries) == len(failed_entries):
            queue_status = "retry_wait"
        elif "failed" in statuses or "retry_wait" in statuses:
            queue_status = "failed"
        elif statuses - {"ready"}:
            queue_status = "downloading"
        else:
            queue_status = ""
        current = next((entry for entry in entries if entry.get("status") == "downloading"), None)
        retry_entries = [entry for entry in entries if int(entry.get("retry_at") or 0) > now]
        terminal_retry_entries = [
            entry for entry in failed_entries if int(entry.get("terminal_retry_at") or 0) > now
        ]
        failed_with_errors = [entry for entry in failed_entries if entry.get("error")]
        latest_failure = max(
            failed_with_errors,
            key=lambda entry: int(entry.get("failed_at") or 0),
            default=None,
        )
        last_error = str(
            (latest_failure or {}).get("error")
            or item.get("publication_error")
            or ""
        )
        failure_class = str((latest_failure or {}).get("failure_class") or "")
        return {
            "queue_status": queue_status,
            "current_file": (current or {}).get("current_file") or item.get("active_file"),
            "download_rate_bps": int((current or {}).get("download_rate_bps") or 0),
            "retry_count": max((int(entry.get("attempts") or 0) for entry in entries), default=0),
            "retry_at": min((int(entry.get("retry_at") or 0) for entry in retry_entries), default=0),
            "next_retry_at": min(
                [int(entry.get("retry_at") or 0) for entry in retry_entries]
                + [int(entry.get("terminal_retry_at") or 0) for entry in terminal_retry_entries]
                or [0]
            ),
            "failure_class": failure_class,
            "last_error": last_error,
            "last_failure_at": max(
                [int(entry.get("failed_at") or 0) for entry in failed_entries] or [0]
            ),
            "queue_updated_at": str(item.get("updated_at") or state.get("updated_at") or ""),
            "task_priority": any(entry.get("priority") == "task" and entry.get("status") != "ready" for entry in entries),
            "queue_required_files": sum(1 for entry in entries if entry.get("priority") == "task"),
            "queue_message": last_error,
        }

    @staticmethod
    def _failure_class(status: int, error: str) -> tuple[str, bool]:
        value = str(error or "").lower()
        if status == 0 or re.search(
            r"curl:\s*\((?:6|7|18|28|35|52|56)\)|could not resolve|timed out|timeout|"
            r"temporary failure|connection (?:reset| refused|closed)",
            value,
        ):
            return "network", True
        if status in {408, 425, 429, 500, 502, 503, 504}:
            return "upstream_temporary", True
        if re.search(r"syntaxerror|future feature|module not found|not executable|command not found", value):
            return "runtime_compatibility", False
        if re.search(r"permission denied|read-only|no space left|storage|mount", value):
            return "storage", False
        if re.search(r"grib|checksum|invalid|corrupt|7777", value):
            return "data_integrity", False
        if status >= 400:
            return "upstream_permanent", False
        return "unknown", False

    @staticmethod
    def _retained_complete_cycles(cycles: list[dict[str, Any]], count: int) -> set[str]:
        """Keep the newest complete fallback cycles while a new target fills."""
        complete = sorted(
            {
                str(item.get("cycle"))
                for item in cycles
                if item.get("complete")
                and SAFE_CYCLE.fullmatch(str(item.get("cycle") or ""))
            },
            reverse=True,
        )
        return set(complete[: max(1, int(count))])

    @staticmethod
    def _is_terminal_pool_message(message: str) -> bool:
        """Recognize failed launcher/download logs even when the queue state is gone."""
        value = str(message or "").strip()
        if not value:
            return False
        return bool(
            re.search(
                r"(?i)(?:^error(?:\||:|\b)|curl:\s*\(\d+\)|syntaxerror|traceback|"
                r"no such file|permission denied|\bfatal\b|\bfailed\b|\bfailure\b)",
                value,
            )
        )

    def _cleanup_history_command(
        self,
        root: str,
        source: str,
        cycle: str,
        size_bytes: int,
        reason: str,
        failure_class: str = "",
        last_error: str = "",
    ) -> str:
        history_path = self._quote_remote(f"{root}/.pool-cleanup-history.log")
        tmp_pattern = self._quote_remote(f"{root}/.pool-cleanup-history.XXXXXX")
        safe_error = str(last_error or "").replace("|", " ").replace("\r", " ").replace("\n", " ")[:240]
        return (
            f"history_path={history_path}; now=$(date -u +%s 2>/dev/null || date +%s); "
            f"printf '%s|%s|%s|%s|%s|%s|%s\\n' \"$now\" {shlex.quote(source)} {shlex.quote(cycle)} "
            f"{shlex.quote(reason)} {int(size_bytes)} {shlex.quote(failure_class)} {shlex.quote(safe_error)} >> \"$history_path\" 2>/dev/null || true; "
            f"cutoff=$((now - {self.settings.hpc_pool_history_retention_days} * 86400)); "
            f"if test -s \"$history_path\" && tmp=$(mktemp {tmp_pattern} 2>/dev/null); then "
            "awk -F'|' -v cutoff=\"$cutoff\" '$1 ~ /^[0-9]+$/ && $1 >= cutoff' \"$history_path\" > \"$tmp\" "
            "&& mv -- \"$tmp\" \"$history_path\" || rm -f -- \"$tmp\"; fi"
        )

    def _forget_queue_command(self, source: str, script: str, root: str, cycle: str) -> str:
        queue_script = self._quote_remote(
            f"{posixpath.dirname(script)}/forcing_pool_queue.py"
        )
        return (
            f"if test -r {queue_script} && command -v python3 >/dev/null 2>&1; then "
            f"python3 {queue_script} forget --source {shlex.quote(source)} "
            f"--root {self._quote_remote(root)} --cycle {shlex.quote(cycle)} >/dev/null 2>&1 || true; fi"
        )

    def _prune_cleanup_history(self, root: str) -> None:
        history_path = self._quote_remote(f"{root}/.pool-cleanup-history.log")
        tmp_pattern = self._quote_remote(f"{root}/.pool-cleanup-history.XXXXXX")
        command = (
            f"now=$(date -u +%s 2>/dev/null || date +%s); cutoff=$((now - {self.settings.hpc_pool_history_retention_days} * 86400)); "
            f"if test -s {history_path} && tmp=$(mktemp {tmp_pattern} 2>/dev/null); then "
            "awk -F'|' -v cutoff=\"$cutoff\" '$1 ~ /^[0-9]+$/ && $1 >= cutoff' "
            f"{history_path} > \"$tmp\" && mv -- \"$tmp\" {history_path} || rm -f -- \"$tmp\"; fi"
        )
        self.run(command, timeout=60)

    def _queue_has_terminal_failure(self, data_source: str, cycle: str, forecast_hours: list[int]) -> str | None:
        spec = self._forcing_settings(data_source)
        state = self._pool_queue_state(str(spec["root"]))
        item = (state.get("cycles") or {}).get(cycle)
        if not isinstance(item, dict):
            return None
        files = item.get("files") or {}
        failures = [
            f"f{hour:03d}: {str((files.get(f'{hour:03d}') or {}).get('error') or 'retry limit reached')}"
            for hour in forecast_hours
            if isinstance(files.get(f"{hour:03d}"), dict) and files[f"{hour:03d}"].get("status") == "failed"
        ]
        return "; ".join(failures) if failures else None

    def complete_forcing_cycles(self, data_source: str) -> list[str]:
        """Return every locally complete managed cycle, newest first."""
        source = self._normalize_forcing_source(data_source)
        pool = self.forcing_pool_items(source)[0]
        return sorted(
            [str(item["cycle"]) for item in pool.get("cycles") or [] if item.get("complete")],
            reverse=True,
        )

    def inspect_forcing_files(self, data_source: str, cycle: str, forecast_hours: list[int]) -> dict[str, Any]:
        source = self._normalize_forcing_source(data_source)
        if source == "gfs":
            result = self.inspect_gfs_files(cycle, forecast_hours)
            result["provider"] = "gfs"
            return result
        self._assert_cycle(cycle)
        spec = self._forcing_settings(source)
        requested = sorted(set(int(hour) for hour in forecast_hours))
        remote_dir = f"{spec['root']}/{cycle}"
        manifest_path = f"{remote_dir}/manifest.json"
        raw = self.run(
            f"if test -s {self._quote_remote(manifest_path)}; then cat {self._quote_remote(manifest_path)}; else printf '{{}}'; fi",
            timeout=60,
        )
        try:
            manifest = json.loads(raw or "{}")
        except json.JSONDecodeError:
            manifest = {}
        candidates: dict[str, dict[str, Any]] = {}
        if isinstance(manifest, dict) and manifest.get("cycle") == cycle:
            for item in manifest.get("files", []):
                entry = self._manifest_entry_forcing(source, item, cycle)
                if entry is not None:
                    candidates[entry["name"]] = entry
        queue_files = ((self._pool_queue_state(str(spec["root"])).get("cycles") or {}).get(cycle) or {}).get("files") or {}
        expected = {spec["file_name"](hour): hour for hour in requested}
        actual_sizes: dict[str, int] = {}
        if expected:
            names = " ".join(shlex.quote(name) for name in sorted(expected))
            command = (
                f"cd {self._quote_remote(remote_dir)} 2>/dev/null || exit 0; for name in {names}; do "
                "if test -f \"$name\" && test \"$(head -c 4 \"$name\" 2>/dev/null)\" = GRIB "
                "&& test \"$(tail -c 4 \"$name\" 2>/dev/null)\" = 7777; then "
                "size=$(wc -c < \"$name\"); printf '%s|%s\\n' \"$name\" \"$size\"; fi; done"
            )
            for line in self.run(command, timeout=300).splitlines():
                name, separator, size_text = line.partition("|")
                if not separator or name not in expected:
                    continue
                try:
                    size = int(size_text)
                except ValueError:
                    continue
                if size >= int(spec["min_bytes"]):
                    actual_sizes[name] = size
        valid_entries: list[dict[str, Any]] = []
        unhashed: list[str] = []
        for name, hour in expected.items():
            size = actual_sizes.get(name)
            if size is None:
                continue
            candidate = candidates.get(name)
            if candidate and size == candidate["size"]:
                valid_entries.append(candidate)
            elif isinstance(queue_files.get(f"{hour:03d}"), dict):
                queue_entry = queue_files[f"{hour:03d}"]
                digest = str(queue_entry.get("sha256") or "").lower()
                if queue_entry.get("status") == "ready" and int(queue_entry.get("bytes") or 0) == size and SHA256.fullmatch(digest):
                    valid_entries.append({"name": name, "forecast_hour": hour, "size": size, "sha256": digest})
                else:
                    unhashed.append(name)
            else:
                unhashed.append(name)
        if unhashed:
            names = " ".join(shlex.quote(name) for name in sorted(unhashed))
            command = (
                f"cd {self._quote_remote(remote_dir)} || exit 1; for name in {names}; do "
                "sha=$(sha256sum \"$name\" | awk '{print $1}'); printf '%s|%s\\n' \"$name\" \"$sha\"; done"
            )
            for line in self.run(command, timeout=1800).splitlines():
                name, separator, digest = line.partition("|")
                if not separator or name not in expected or not SHA256.fullmatch(digest.lower()):
                    continue
                valid_entries.append(
                    {"name": name, "forecast_hour": expected[name], "size": actual_sizes[name], "sha256": digest.lower()}
                )
        valid_by_hour = {entry["forecast_hour"]: entry for entry in valid_entries}
        valid_hours = [hour for hour in requested if hour in valid_by_hour]
        missing_hours = [hour for hour in requested if hour not in valid_by_hour]
        return {
            "provider": source,
            "complete": not missing_hours,
            "requested_hours": requested,
            "valid_hours": valid_hours,
            "missing_hours": missing_hours,
            "entries": sorted(valid_entries, key=lambda item: item["forecast_hour"]),
            "remote_dir": remote_dir,
            "scope": spec["scope"],
            "legacy_imported_hours": [],
            "manifest_needs_rebuild": bool(unhashed),
            "manifest_is_full": bool(candidates),
            "manifest_is_regional": bool(candidates),
        }

    def _manifest_entry_forcing(self, data_source: str, item: Any, cycle: str) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "")
        matcher = SAFE_GFS_NAME if data_source == "gfs" else SAFE_EC_NAME
        match = matcher.fullmatch(name)
        digest = str(item.get("sha256") or "").lower()
        try:
            hour = int(item.get("forecast_hour"))
            size = int(item.get("size"))
        except (TypeError, ValueError):
            return None
        if not match or name.split(".t", 1)[1][:2] != cycle[8:10] or int(match.group(1)) != hour or size <= 0:
            return None
        if not SHA256.fullmatch(digest):
            return None
        return {"name": name, "forecast_hour": hour, "size": size, "sha256": digest}

    def trigger_forcing_download(
        self,
        data_source: str,
        cycle: str,
        horizon: int = 72,
        forecast_hours: list[int] | None = None,
    ) -> dict[str, Any]:
        source = self._normalize_forcing_source(data_source)
        if source == "gfs":
            return self.trigger_gfs_download(cycle, horizon, forecast_hours)
        self._assert_cycle(cycle)
        if cycle[8:10] != "00":
            raise ValueError("当前 WRF 数据池仅允许触发 00Z 周期")
        spec = self._forcing_settings(source)
        horizon = max(0, min(72, int(horizon)))
        if forecast_hours is None:
            requested_hours = [hour for hour in spec["hours"] if hour <= horizon]
        else:
            requested_hours = sorted({int(hour) for hour in forecast_hours})
        if not requested_hours or requested_hours[0] < 0 or requested_hours[-1] > 72:
            raise ValueError("ECMWF 下载时次必须位于 f000-f072")
        invalid_hours = [hour for hour in requested_hours if hour not in spec["hours"]]
        if invalid_hours:
            raise ValueError(
                "ECMWF 全球开放资料仅提供 3 小时时次，请使用 f000、f003…f072；"
                f"无效时次：{','.join(f'f{hour:03d}' for hour in invalid_hours)}"
            )
        requested_spec = ",".join(str(hour) for hour in requested_hours)
        intent = "task" if forecast_hours is not None else "prefetch"
        remote_parent = posixpath.dirname(spec["script"])
        self.run(f"mkdir -p {self._quote_remote(remote_parent)}", timeout=60)
        local_queue_script = Path(__file__).resolve().parent / "scripts" / "forcing_pool_queue.py"
        self.upload(local_queue_script, f"{remote_parent}/forcing_pool_queue.py", timeout=300)
        self.upload(spec["local_script"], spec["script"], timeout=300)
        self.run(
            f"chmod 700 {self._quote_remote(spec['script'])} "
            f"&& chmod 700 {self._quote_remote(f'{remote_parent}/forcing_pool_queue.py')}",
            timeout=60,
        )
        log_dir = f"{spec['root']}/logs"
        log_path = f"{log_dir}/download_{cycle}.out"
        command = (
            f"mkdir -p {self._quote_remote(spec['root'])} {self._quote_remote(log_dir)} || "
            f"{{ printf 'ERROR|ecmwf_storage_io_error'; exit 0; }}; "
            f"env WRF_EC_DATA_ROOT={self._quote_remote(spec['root'])} "
            f"WRF_POOL_RETRY_MAX_ATTEMPTS={self.settings.hpc_pool_retry_max_attempts} "
            f"WRF_POOL_RETRY_MAX_SECONDS={self.settings.hpc_pool_retry_max_seconds} "
            f"WRF_POOL_PUBLISH_PROBE_SECONDS={self.settings.hpc_pool_publish_probe_seconds} "
            f"WRF_POOL_HISTORY_RETENTION_DAYS={self.settings.hpc_pool_history_retention_days} "
            f"bash {self._quote_remote(spec['script'])} {cycle} {horizon} {self._quote_remote(requested_spec)} {intent} "
            f"> {self._quote_remote(log_path)} 2>&1; tail -n 1 {self._quote_remote(log_path)}"
        )
        state, _separator, detail = self.run(command, timeout=60).partition("|")
        if state == "ERROR":
            raise HpcError(f"tx-lab ECMWF 系统盘目录不可写：{spec['root']}")
        if state == "QUEUED":
            return {"cycle": cycle, "status": "queued", "detail": f"{intent}:{detail or requested_spec}"}
        return {"cycle": cycle, "status": state.lower(), "detail": detail}

    def ensure_remote_forcing(
        self,
        data_source: str,
        cycle: str,
        forecast_hours: list[int],
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        protected_cycles: set[str] | None = None,
    ) -> dict[str, Any]:
        source = self._normalize_forcing_source(data_source)
        if source == "gfs":
            return self.ensure_remote_gfs(cycle, forecast_hours, progress, cancelled, protected_cycles)
        spec = self._forcing_settings(source)
        result = self.inspect_forcing_files(source, cycle, forecast_hours)
        if result["complete"]:
            return result
        trigger = self.trigger_forcing_download(source, cycle, 72, forecast_hours=forecast_hours)
        if trigger.get("status") in {"failed", "error"}:
            raise HpcError(f"tx-lab ECMWF {cycle} 下载启动失败：{trigger.get('detail') or 'unknown'}")
        if progress:
            progress({**result, "download": trigger})
        while True:
            if cancelled and cancelled():
                raise HpcError("任务已取消等待；tx-lab 共享 ECMWF 下载继续运行")
            time.sleep(int(spec["poll_seconds"]))
            result = self.inspect_forcing_files(source, cycle, forecast_hours)
            if progress:
                progress({**result, "download": trigger})
            if result["complete"]:
                return result
            failure = self._queue_has_terminal_failure(source, cycle, result.get("missing_hours") or forecast_hours)
            if failure:
                raise HpcError(f"tx-lab ECMWF {cycle} 必需驱动文件下载失败：{failure}")

    def gfs_pool_items(
        self,
        target_cycles: list[str] | None = None,
        protected_cycles: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        targets = set(target_cycles or [])
        protected = set(protected_cycles or [])
        for cycle in targets | protected:
            self._assert_cycle(cycle)
        root = self.settings.hpc_gfs_dir
        queue_state = self._pool_queue_state(root)
        cycle_feed = ""
        listed_cycles = targets | protected
        if listed_cycles:
            cycle_feed = "printf '%s\\n' " + " ".join(
                shlex.quote(cycle) for cycle in sorted(listed_cycles)
            ) + "; "
        output = self.run(
            f"cd {self._quote_remote(root)} 2>/dev/null || exit 0; "
            "root=$(pwd -P); printf 'ROOT|%s\\n' \"$root\"; "
            f"{{ find . -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'; {cycle_feed}}} | "
            "awk '/^[0-9]{10}$/' | sort -r -u | while read cycle; do "
            "dir=\"$cycle\"; count=$(find \"$dir\" -maxdepth 1 -type f -name 'gfs.t00z.pgrb2.0p25.f???' 2>/dev/null | wc -l); "
            "size=$(find \"$dir\" -maxdepth 1 -type f -name 'gfs.t00z.pgrb2.0p25.f???' -printf '%s\\n' 2>/dev/null | awk '{s+=$1} END {print s+0}'); "
            "partial_stats=$(find \"$dir\" -maxdepth 1 -type f -name 'gfs.t00z.pgrb2.0p25.f???.part*' -printf '%f|%s\\n' 2>/dev/null | "
            "awk -F'|' '{key=$1; sub(/\\.part.*/, \"\", key); if ($2 > largest[key]) largest[key]=$2} "
            "END {for (key in largest) {count+=1; size+=largest[key]} printf \"%d %d\", count, size}'); "
            "partial_count=${partial_stats%% *}; partial_size=${partial_stats#* }; "
            "last=$(find \"$dir\" -maxdepth 1 -type f -name 'gfs.t00z.pgrb2.0p25.f???' -printf '%f\\n' 2>/dev/null | sed -n 's/.*f\\([0-9][0-9][0-9]\\)$/\\1/p' | sort -n | tail -1); "
            "complete=1; hour=0; while test \"$hour\" -le 72; do fh=$(printf '%03d' \"$hour\"); "
            "test -s \"$dir/gfs.t00z.pgrb2.0p25.f$fh\" || complete=0; hour=$((hour + 1)); done; "
            "log=\"logs/download_${cycle}.out\"; message=''; "
            "test -s \"$log\" && message=$(tail -n 1 \"$log\" | tr '|\\r\\n' '   ' | cut -c1-240); "
            "active=0; if test \"$complete\" -eq 1; then state=ready; else state=partial; fi; "
            "if test \"$state\" = partial && printf '%s' \"$message\" | grep -q '^ERROR'; then state=error; "
            "elif test \"$state\" = partial && test ! -d \"$dir\"; then state=missing; fi; "
            "printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \"$cycle\" \"$state\" \"$count\" \"$size\" \"${last:-0}\" \"$partial_count\" \"$partial_size\" \"$active\" \"$message\"; done",
            timeout=120,
        )
        remote_root = ""
        cycles: list[dict[str, Any]] = []
        for line in output.splitlines():
            if line.startswith("ROOT|"):
                remote_root = line.partition("|")[2].strip().rstrip("/")
                continue
            parts = line.split("|", 8)
            if len(parts) not in {5, 6, 9} or not SAFE_CYCLE.fullmatch(parts[0]):
                continue
            cycle, status, count, size, last = parts[:5]
            if len(parts) == 9:
                partial_count, partial_size, active_downloads = parts[5:8]
                download_message = parts[8].strip()
            else:
                partial_count = partial_size = active_downloads = "0"
                download_message = parts[5].strip() if len(parts) == 6 else ""
            queue_details = self._queue_cycle_details(queue_state, cycle)
            effective_status = status
            if status != "ready" and queue_details.get("queue_status"):
                effective_status = str(queue_details["queue_status"])
            if (
                effective_status not in {"ready", "downloading", "rate_limited", "waiting_publish", "retry_wait"}
                and self._is_terminal_pool_message(download_message)
            ):
                effective_status = "error"
            empty_residue = (
                int(count) == 0
                and int(partial_count) == 0
                and not queue_details.get("queue_status")
                and cycle not in targets
                and cycle not in protected
            )
            if empty_residue:
                effective_status = "error"
                download_message = download_message or "empty cycle directory"
            terminal_failure = effective_status in {"failed", "error"} or empty_residue
            if effective_status == "retry_wait" and cycle not in targets and cycle not in protected:
                terminal_failure = True
            last_error = queue_details.get("last_error") or download_message
            failure_class = queue_details.get("failure_class") or ""
            if empty_residue:
                failure_class = "empty_residue"
            elif failure_class in {"", "unknown"} and terminal_failure:
                failure_class = self._failure_class(-1, last_error)[0]
            cycles.append(
                {
                    "cycle": cycle,
                    "status": effective_status,
                    "forecast_start": 0 if int(count) else None,
                    "forecast_end": int(last) if int(count) else None,
                    "completed_files": int(count),
                    "total_files": 73,
                    "size_bytes": int(size),
                    "partial_files": int(partial_count),
                    "partial_size_bytes": int(partial_size),
                    "active_downloads": int(active_downloads),
                    "complete": status == "ready",
                    "remote_path": f"{remote_root}/{cycle}" if remote_root else "",
                    "target": cycle in targets,
                    "protected": cycle in protected,
                    "download_message": download_message,
                    "empty_residue": empty_residue,
                    **queue_details,
                    "terminal_failure": terminal_failure,
                    "failure_class": failure_class,
                    "last_error": last_error,
                    "next_retry_at": int(queue_details.get("next_retry_at") or 0),
                    "last_failure_at": int(queue_details.get("last_failure_at") or 0),
                    "last_updated_at": queue_details.get("queue_updated_at") or "",
                    "scope": GFS_SCOPE,
                    "bounds": self.gfs_bounds,
                }
            )
        by_cycle = {item["cycle"]: item for item in cycles}
        targets_complete = bool(targets) and all(by_cycle.get(cycle, {}).get("complete") for cycle in targets)
        retained_complete = self._retained_complete_cycles(
            cycles, self.settings.hpc_gfs_retained_cycles
        )
        for item in cycles:
            # 目标、最近保留周期和活动任务周期都不会进入清理候选。
            item["retained_complete"] = item["cycle"] in retained_complete
            item["cleanup_allowed"] = bool(
                not item["target"]
                and not item["protected"]
                and not item["retained_complete"]
                and item["status"] not in {"downloading", "rate_limited", "waiting_publish"}
                and item["remote_path"]
            )
            item["immediate_cleanup_allowed"] = bool(
                item["cleanup_allowed"] and item["terminal_failure"]
            )
            item["auto_cleanup_allowed"] = bool(
                item["immediate_cleanup_allowed"]
                or (item["cleanup_allowed"] and item["complete"])
            )
            if item["target"] or item["protected"]:
                item["cleanup_state"] = "protected"
                item["cleanup_reason"] = "target_or_active_task"
            elif item["retained_complete"]:
                item["cleanup_state"] = "protected"
                item["cleanup_reason"] = "complete_fallback_window"
            elif item["status"] in {"downloading", "rate_limited", "waiting_publish"}:
                item["cleanup_state"] = "active"
                item["cleanup_reason"] = "download_in_progress"
            elif item["immediate_cleanup_allowed"]:
                item["cleanup_state"] = "candidate"
                item["cleanup_reason"] = "empty_residue" if item.get("empty_residue") else "terminal_failure"
            elif item["auto_cleanup_allowed"]:
                item["cleanup_state"] = "candidate"
                item["cleanup_reason"] = "obsolete_cycle"
            else:
                item["cleanup_state"] = "retained"
                item["cleanup_reason"] = "fallback_until_targets_ready"
        if any(item["status"] in {"failed", "error"} and item["target"] for item in cycles):
            overall = "failed"
        elif any(item["status"] == "retry_wait" and item["target"] for item in cycles):
            overall = "retry_wait"
        elif any(item["status"] == "rate_limited" for item in cycles):
            overall = "rate_limited"
        elif any(item["status"] == "downloading" for item in cycles):
            overall = "downloading"
        elif any(item["status"] == "waiting_publish" for item in cycles):
            overall = "waiting_publish"
        elif targets_complete:
            overall = "ready"
        elif cycles:
            overall = "partial"
        else:
            overall = "idle"
        cleanup_candidates = [item["remote_path"] for item in cycles if item["cleanup_allowed"]]
        terminal_cleanup_candidates = [
            item["remote_path"] for item in cycles if item["immediate_cleanup_allowed"]
        ]
        auto_cleanup_candidates = [
            item["remote_path"] for item in cycles if item["auto_cleanup_allowed"]
        ]
        return [{
            "provider": "gfs",
            "label": "GFS 全球 0.25°",
            "source": "NOAA NOMADS Grib Filter",
            "scope": GFS_SCOPE,
            "bounds": self.gfs_bounds,
            "storage_path": self.settings.hpc_gfs_dir,
            "status": overall,
            "remote_root": remote_root,
            "target_cycles": list(target_cycles or []),
            "targets_complete": targets_complete,
            "retained_complete_cycles": sorted(retained_complete, reverse=True),
            "cleanup_candidates": cleanup_candidates,
            "terminal_cleanup_candidates": terminal_cleanup_candidates,
            "auto_cleanup_candidates": auto_cleanup_candidates,
            "cycles": cycles,
        }]

    def forcing_pool_items(
        self,
        data_source: str,
        target_cycles: list[str] | None = None,
        protected_cycles: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        source = self._normalize_forcing_source(data_source)
        if source == "gfs":
            return self.gfs_pool_items(target_cycles, protected_cycles)
        spec = self._forcing_settings(source)
        targets = set(target_cycles or [])
        protected = set(protected_cycles or [])
        for cycle in targets | protected:
            self._assert_cycle(cycle)
        root = spec["root"]
        queue_state = self._pool_queue_state(root)
        cycle_feed = ""
        listed_cycles = targets | protected
        if listed_cycles:
            cycle_feed = "printf '%s\\n' " + " ".join(
                shlex.quote(cycle) for cycle in sorted(listed_cycles)
            ) + "; "
        expected_hours_shell = " ".join(f"{hour}" for hour in spec["hours"])
        expected_files_check = (
            f"complete=1; for hour in {expected_hours_shell}; do fh=$(printf '%03d' \"$hour\"); "
            "test -s \"$dir/ecmwf.t00z.ifs.0p25.f${fh}.grib2\" || complete=0; done; "
        )
        output = self.run(
            f"cd {self._quote_remote(root)} 2>/dev/null || exit 0; "
            "root=$(pwd -P); printf 'ROOT|%s\\n' \"$root\"; "
            f"{{ find . -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'; {cycle_feed}}} | "
            "awk '/^[0-9]{10}$/' | sort -r -u | while read cycle; do "
            f"dir=\"$cycle\"; count=$(find \"$dir\" -maxdepth 1 -type f -name '{spec['file_glob']}' 2>/dev/null | wc -l); "
            f"size=$(find \"$dir\" -maxdepth 1 -type f -name '{spec['file_glob']}' -printf '%s\\n' 2>/dev/null | awk '{{s+=$1}} END {{print s+0}}'); "
            f"partial_stats=$(find \"$dir\" -maxdepth 1 -type f -name '{spec['part_glob']}' -printf '%f|%s\\n' 2>/dev/null | "
            "awk -F'|' '{key=$1; sub(/\\.part.*/, \"\", key); if ($2 > largest[key]) largest[key]=$2} "
            "END {for (key in largest) {count+=1; size+=largest[key]} printf \"%d %d\", count, size}'); "
            "partial_count=${partial_stats%% *}; partial_size=${partial_stats#* }; "
            f"last=$(find \"$dir\" -maxdepth 1 -type f -name '{spec['file_glob']}' -printf '%f\\n' 2>/dev/null | sed -n 's/.*f\\([0-9][0-9][0-9]\\)\\.grib2$/\\1/p' | sort -n | tail -1); "
            + expected_files_check
            + "log=\"logs/download_${cycle}.out\"; message=''; "
            "test -s \"$log\" && message=$(tail -n 1 \"$log\" | tr '|\\r\\n' '   ' | cut -c1-240); "
            "if test \"$complete\" -eq 1; then state=ready; active=0; else state=partial; active=0; fi; "
            "if test \"$state\" = partial && printf '%s' \"$message\" | grep -q '^ERROR'; then state=error; "
            "elif test \"$state\" = partial && test ! -d \"$dir\"; then state=missing; fi; "
            "printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' \"$cycle\" \"$state\" \"$count\" \"$size\" \"${last:-0}\" \"$partial_count\" \"$partial_size\" \"$active\" \"$message\"; done",
            timeout=120,
        )
        remote_root = ""
        cycles: list[dict[str, Any]] = []
        for line in output.splitlines():
            if line.startswith("ROOT|"):
                remote_root = line.partition("|")[2].strip().rstrip("/")
                continue
            parts = line.split("|", 8)
            if len(parts) != 9 or not SAFE_CYCLE.fullmatch(parts[0]):
                continue
            cycle, status, count, size, last, partial_count, partial_size, active_downloads, download_message = parts
            queue_details = self._queue_cycle_details(queue_state, cycle)
            effective_status = status
            if status != "ready" and queue_details.get("queue_status"):
                effective_status = str(queue_details["queue_status"])
            if (
                effective_status not in {"ready", "downloading", "rate_limited", "waiting_publish", "retry_wait"}
                and self._is_terminal_pool_message(download_message)
            ):
                effective_status = "error"
            empty_residue = (
                int(count) == 0
                and int(partial_count) == 0
                and not queue_details.get("queue_status")
                and cycle not in targets
                and cycle not in protected
            )
            if empty_residue:
                effective_status = "error"
                download_message = download_message.strip() or "empty cycle directory"
            terminal_failure = effective_status in {"failed", "error"} or empty_residue
            if effective_status == "retry_wait" and cycle not in targets and cycle not in protected:
                terminal_failure = True
            last_error = queue_details.get("last_error") or download_message.strip()
            failure_class = queue_details.get("failure_class") or ""
            if empty_residue:
                failure_class = "empty_residue"
            elif failure_class in {"", "unknown"} and terminal_failure:
                failure_class = self._failure_class(-1, last_error)[0]
            cycles.append(
                {
                    "cycle": cycle,
                    "status": effective_status,
                    "forecast_start": 0 if int(count) else None,
                    "forecast_end": int(last) if int(count) else None,
                    "completed_files": int(count),
                    "total_files": spec["total_files"],
                    "size_bytes": int(size),
                    "partial_files": int(partial_count),
                    "partial_size_bytes": int(partial_size),
                    "active_downloads": int(active_downloads),
                    "complete": status == "ready",
                    "remote_path": f"{remote_root}/{cycle}" if remote_root else "",
                    "target": cycle in targets,
                    "protected": cycle in protected,
                    "download_message": download_message.strip(),
                    "empty_residue": empty_residue,
                    **queue_details,
                    "terminal_failure": terminal_failure,
                    "failure_class": failure_class,
                    "last_error": last_error,
                    "next_retry_at": int(queue_details.get("next_retry_at") or 0),
                    "last_failure_at": int(queue_details.get("last_failure_at") or 0),
                    "last_updated_at": queue_details.get("queue_updated_at") or "",
                    "scope": spec["scope"],
                }
            )
        by_cycle = {item["cycle"]: item for item in cycles}
        targets_complete = bool(targets) and all(by_cycle.get(cycle, {}).get("complete") for cycle in targets)
        retained_complete = self._retained_complete_cycles(
            cycles, self.settings.hpc_gfs_retained_cycles
        )
        for item in cycles:
            item["retained_complete"] = item["cycle"] in retained_complete
            item["cleanup_allowed"] = bool(
                not item["target"] and not item["protected"]
                and not item["retained_complete"]
                and item["status"] not in {"downloading", "rate_limited", "waiting_publish"}
                and item["remote_path"]
            )
            item["immediate_cleanup_allowed"] = bool(
                item["cleanup_allowed"] and item["terminal_failure"]
            )
            item["auto_cleanup_allowed"] = bool(
                item["immediate_cleanup_allowed"]
                or (item["cleanup_allowed"] and item["complete"])
            )
            if item["target"] or item["protected"]:
                item["cleanup_state"] = "protected"
                item["cleanup_reason"] = "target_or_active_task"
            elif item["retained_complete"]:
                item["cleanup_state"] = "protected"
                item["cleanup_reason"] = "complete_fallback_window"
            elif item["status"] in {"downloading", "rate_limited", "waiting_publish"}:
                item["cleanup_state"] = "active"
                item["cleanup_reason"] = "download_in_progress"
            elif item["immediate_cleanup_allowed"]:
                item["cleanup_state"] = "candidate"
                item["cleanup_reason"] = "empty_residue" if item.get("empty_residue") else "terminal_failure"
            elif item["auto_cleanup_allowed"]:
                item["cleanup_state"] = "candidate"
                item["cleanup_reason"] = "obsolete_cycle"
            else:
                item["cleanup_state"] = "retained"
                item["cleanup_reason"] = "fallback_until_targets_ready"
        if any(item["status"] in {"failed", "error"} and item["target"] for item in cycles):
            overall = "failed"
        elif any(item["status"] == "retry_wait" and item["target"] for item in cycles):
            overall = "retry_wait"
        elif any(item["status"] == "rate_limited" for item in cycles):
            overall = "rate_limited"
        elif any(item["status"] == "downloading" for item in cycles):
            overall = "downloading"
        elif any(item["status"] == "waiting_publish" for item in cycles):
            overall = "waiting_publish"
        elif targets_complete:
            overall = "ready"
        elif cycles:
            overall = "partial"
        else:
            overall = "idle"
        return [{
            "provider": source,
            "label": spec["label"],
            "source": "ECMWF IFS",
            "scope": spec["scope"],
            "storage_path": root,
            "status": overall,
            "remote_root": remote_root,
            "target_cycles": list(target_cycles or []),
            "targets_complete": targets_complete,
            "retained_complete_cycles": sorted(retained_complete, reverse=True),
            "cleanup_candidates": [item["remote_path"] for item in cycles if item["cleanup_allowed"]],
            "terminal_cleanup_candidates": [
                item["remote_path"] for item in cycles if item["immediate_cleanup_allowed"]
            ],
            "auto_cleanup_candidates": [item["remote_path"] for item in cycles if item["auto_cleanup_allowed"]],
            "cycles": cycles,
        }]

    def auto_cleanup_gfs_cycles(
        self,
        target_cycles: list[str],
        protected_cycles: set[str],
        *,
        terminal_only: bool = False,
    ) -> dict[str, Any]:
        """Clean terminal failures immediately; clean usable history after targets complete."""
        pool = self.gfs_pool_items(target_cycles, protected_cycles)[0]
        if pool.get("remote_root"):
            self._prune_cleanup_history(str(pool["remote_root"]))
        candidate_key = "terminal_cleanup_candidates" if terminal_only else "auto_cleanup_candidates"
        requested = list(pool.get(candidate_key) or [])
        if not requested:
            return {"requested": [], "deleted": [], "missing": []}
        result = self.cleanup_gfs_cycles(requested, target_cycles, protected_cycles)
        return {"requested": requested, **result}

    def auto_cleanup_forcing_cycles(
        self,
        data_source: str,
        target_cycles: list[str],
        protected_cycles: set[str],
        *,
        terminal_only: bool = False,
    ) -> dict[str, Any]:
        source = self._normalize_forcing_source(data_source)
        if source == "gfs":
            return self.auto_cleanup_gfs_cycles(
                target_cycles, protected_cycles, terminal_only=terminal_only
            )
        pool = self.forcing_pool_items(source, target_cycles, protected_cycles)[0]
        if pool.get("remote_root"):
            self._prune_cleanup_history(str(pool["remote_root"]))
        candidate_key = "terminal_cleanup_candidates" if terminal_only else "auto_cleanup_candidates"
        requested = list(pool.get(candidate_key) or [])
        if not requested:
            return {"requested": [], "deleted": [], "missing": []}
        result = self.cleanup_forcing_cycles(source, requested, target_cycles, protected_cycles)
        return {"requested": requested, **result}

    def cleanup_forcing_cycles(
        self,
        data_source: str,
        paths: list[str],
        target_cycles: list[str],
        protected_cycles: set[str],
    ) -> dict[str, Any]:
        source = self._normalize_forcing_source(data_source)
        if source == "gfs":
            return self.cleanup_gfs_cycles(paths, target_cycles, protected_cycles)
        spec = self._forcing_settings(source)
        requested = list(dict.fromkeys(str(path).rstrip("/") for path in paths))
        if not requested:
            raise ValueError("至少选择一个待清理的 tx-lab ECMWF 周期")
        pool = self.forcing_pool_items(source, target_cycles, protected_cycles)[0]
        allowed = set(pool.get("cleanup_candidates") or [])
        refused = [path for path in requested if path not in allowed]
        if refused:
            raise ValueError(f"以下路径当前不允许清理：{', '.join(refused)}")
        root = str(pool.get("remote_root") or "").rstrip("/")
        if not root:
            raise HpcError("无法确认 tx-lab ECMWF 数据池绝对路径")
        commands: list[str] = []
        cycle_items = {item["remote_path"]: item for item in pool.get("cycles") or []}
        for path in requested:
            cycle = posixpath.basename(path)
            if not SAFE_CYCLE.fullmatch(cycle) or posixpath.dirname(path) != root:
                raise ValueError(f"拒绝清理非受管 ECMWF 周期目录：{path}")
            quoted_path = shlex.quote(path)
            cycle_lock = self._quote_remote(f"{root}/.pool-cycle-{cycle}.lock")
            remove_cycle = shlex.quote(f"rm -rf -- {quoted_path}")
            pattern = shlex.quote(f"[d]ownload_ecmwf_00z.sh {cycle}")
            item = cycle_items.get(path) or {}
            reason = (
                "empty_residue" if item.get("empty_residue")
                else "terminal_failure" if item.get("terminal_failure")
                else "obsolete_cycle"
            )
            size_bytes = int(item.get("size_bytes") or 0) + int(item.get("partial_size_bytes") or 0)
            forget = self._forget_queue_command("ecmwf", spec["script"], root, cycle)
            history = self._cleanup_history_command(
                root, "ecmwf", cycle, size_bytes, reason,
                str(item.get("failure_class") or ""), str(item.get("last_error") or ""),
            )
            log_path = self._quote_remote(f"{root}/logs/download_{cycle}.out")
            commands.append(
                f"if ! command -v flock >/dev/null 2>&1; then printf 'BLOCKED|%s\\n' {quoted_path}; "
                f"elif pgrep -f {pattern} >/dev/null 2>&1; then "
                f"printf 'BLOCKED|%s\\n' {quoted_path}; "
                f"elif test -d {quoted_path}; then if flock -n {cycle_lock} -c {remove_cycle}; then {forget}; rm -f -- {log_path}; {history}; printf 'DELETED|%s\\n' {quoted_path}; else printf 'BLOCKED|%s\\n' {quoted_path}; fi; "
                f"else {forget}; rm -f -- {log_path}; printf 'MISSING|%s\\n' {quoted_path}; fi"
            )
        output = self.run("; ".join(commands), timeout=120)
        deleted: list[str] = []
        missing: list[str] = []
        blocked: list[str] = []
        errors: list[str] = []
        for line in output.splitlines():
            state, separator, path = line.partition("|")
            if not separator or path not in requested:
                continue
            if state == "DELETED":
                deleted.append(path)
            elif state == "MISSING":
                missing.append(path)
            elif state == "BLOCKED":
                blocked.append(path)
            elif state == "ERROR":
                errors.append(path)
        if blocked:
            raise HpcError(f"周期下载进程仍在运行，未清理：{', '.join(blocked)}")
        if errors:
            raise HpcError(f"周期目录删除失败，未清理：{', '.join(errors)}")
        return {"deleted": deleted, "missing": missing}

    def cleanup_gfs_cycles(
        self,
        paths: list[str],
        target_cycles: list[str],
        protected_cycles: set[str],
    ) -> dict[str, Any]:
        """只删除数据池当前明确标记为可清理的精确周期路径。"""
        requested = list(dict.fromkeys(str(path).rstrip("/") for path in paths))
        if not requested:
            raise ValueError("至少选择一个待清理的 tx-lab GFS 周期")
        pool = self.gfs_pool_items(target_cycles, protected_cycles)[0]
        allowed = set(pool.get("cleanup_candidates") or [])
        refused = [path for path in requested if path not in allowed]
        if refused:
            raise ValueError(f"以下路径当前不允许清理：{', '.join(refused)}")
        root = str(pool.get("remote_root") or "").rstrip("/")
        if not root:
            raise HpcError("无法确认 tx-lab GFS 数据池绝对路径")
        commands: list[str] = []
        cycle_items = {item["remote_path"]: item for item in pool.get("cycles") or []}
        for path in requested:
            cycle = posixpath.basename(path)
            if not SAFE_CYCLE.fullmatch(cycle) or posixpath.dirname(path) != root:
                raise ValueError(f"拒绝清理非受管 GFS 周期目录：{path}")
            quoted_path = shlex.quote(path)
            cycle_lock = self._quote_remote(f"{root}/.pool-cycle-{cycle}.lock")
            remove_cycle = shlex.quote(f"rm -rf -- {quoted_path}")
            pattern = shlex.quote(f"[d]ownload_gfs_00z.sh {cycle}")
            item = cycle_items.get(path) or {}
            reason = (
                "empty_residue" if item.get("empty_residue")
                else "terminal_failure" if item.get("terminal_failure")
                else "obsolete_cycle"
            )
            size_bytes = int(item.get("size_bytes") or 0) + int(item.get("partial_size_bytes") or 0)
            forget = self._forget_queue_command("gfs", self.settings.hpc_gfs_download_script, root, cycle)
            history = self._cleanup_history_command(
                root, "gfs", cycle, size_bytes, reason,
                str(item.get("failure_class") or ""), str(item.get("last_error") or ""),
            )
            log_path = self._quote_remote(f"{root}/logs/download_{cycle}.out")
            commands.append(
                f"if ! command -v flock >/dev/null 2>&1; then printf 'BLOCKED|%s\\n' {quoted_path}; "
                f"elif pgrep -f {pattern} >/dev/null 2>&1; then "
                f"printf 'BLOCKED|%s\\n' {quoted_path}; "
                f"elif test -d {quoted_path}; then if flock -n {cycle_lock} -c {remove_cycle}; then {forget}; rm -f -- {log_path}; {history}; printf 'DELETED|%s\\n' {quoted_path}; else printf 'BLOCKED|%s\\n' {quoted_path}; fi; "
                f"else {forget}; rm -f -- {log_path}; printf 'MISSING|%s\\n' {quoted_path}; fi"
            )
        output = self.run("; ".join(commands), timeout=120)
        deleted: list[str] = []
        missing: list[str] = []
        blocked: list[str] = []
        errors: list[str] = []
        for line in output.splitlines():
            state, separator, path = line.partition("|")
            if not separator or path not in requested:
                continue
            if state == "DELETED":
                deleted.append(path)
            elif state == "MISSING":
                missing.append(path)
            elif state == "BLOCKED":
                blocked.append(path)
            elif state == "ERROR":
                errors.append(path)
        if blocked:
            raise HpcError(f"周期下载进程仍在运行，未清理：{', '.join(blocked)}")
        if errors:
            raise HpcError(f"周期目录删除失败，未清理：{', '.join(errors)}")
        return {"deleted": deleted, "missing": missing}

    def analyze_geography(self, fingerprint: str, request: dict[str, Any]) -> dict[str, Any]:
        """在超算运行/复用 geogrid，并用 ncdump+awk 汇总地形与下垫面。"""
        if not SAFE_FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("非法地理分析指纹")
        domains = list(request.get("domains") or [])
        if not domains:
            raise ValueError("地理分析缺少嵌套域")
        center = request["center"]
        start = str(request.get("start_time") or "2020-01-01T00:00:00Z")
        stamp = start[:13].replace("T", "_") + ":00:00"
        repeat = lambda value: ", ".join(str(value) for _ in domains)
        quoted_dates = repeat(f"'{stamp}'")
        parent_ids = ", ".join(str(1 if index == 0 else int(item.get("parent_id", index))) for index, item in enumerate(domains))
        ratios = ", ".join(str(int(item.get("parent_grid_ratio", 1))) for item in domains)
        i_starts = ", ".join(str(int(item.get("i_parent_start", 1))) for item in domains)
        j_starts = ", ".join(str(int(item.get("j_parent_start", 1))) for item in domains)
        e_we = ", ".join(str(int(item["e_we"])) for item in domains)
        e_sn = ", ".join(str(int(item["e_sn"])) for item in domains)
        namelist = f"""&share
 wrf_core='ARW', max_dom={len(domains)},
 start_date={quoted_dates}, end_date={quoted_dates}, interval_seconds=21600,
 io_form_geogrid=2, debug_level=0,
/
&geogrid
 parent_id={parent_ids}, parent_grid_ratio={ratios},
 i_parent_start={i_starts}, j_parent_start={j_starts},
 e_we={e_we}, e_sn={e_sn}, geog_data_res={repeat("'default'")},
 dx={int(domains[0]['dx'])}, dy={int(domains[0]['dy'])}, map_proj='lambert',
 ref_x={center.get('grid_x') or (int(domains[0]['e_we']) + 1) / 2}, ref_y={center.get('grid_y') or (int(domains[0]['e_sn']) + 1) / 2},
 ref_lat={float(center['lat'])}, ref_lon={float(center['lon'])},
 truelat1={float(center['lat'])}, truelat2={float(center['lat'])},
 pole_lat=90, pole_lon=0, stand_lon={float(center['lon'])},
 geog_data_path='{self.settings.hpc_geog_dir}', opt_geogrid_tbl_path='./geogrid/',
/
&ungrib
 out_format='WPS', prefix='FILE',
/
&metgrid
 fg_name='FILE', io_form_metgrid=2, opt_metgrid_tbl_path='./metgrid',
/
"""
        remote_dir = f"{self.settings.hpc_remote_dir}/backend_wrf_geo/{fingerprint}"
        self.run(f"mkdir -p {self._quote_remote(remote_dir)}", timeout=60)
        self._upload_text_atomic(namelist, f"{remote_dir}/namelist.wps")
        expected = " ".join(f"geo_em.d{index:02d}.nc" for index in range(1, len(domains) + 1))
        module_setup = f". {self._quote_remote(self.settings.hpc_runtime_env)} || exit 21; "
        command = (
            f"cd {self._quote_remote(remote_dir)} || exit 1; {module_setup} "
            f"ready=1; for f in {expected}; do test -s \"$f\" || ready=0; done; "
            "if test \"$ready\" -ne 1; then "
            f"test -d geogrid || cp -r {self._quote_remote(self.settings.hpc_wps_source_dir)}/geogrid ./geogrid; "
            f"ln -sf {self._quote_remote(self.settings.hpc_wps_source_dir)}/geogrid.exe ./geogrid.exe; "
            "./geogrid.exe > geogrid.log 2>&1 || { tail -40 geogrid.log; exit 22; }; fi; "
            "for f in geo_em.d??.nc; do test -s \"$f\" || continue; dom=${f#geo_em.}; dom=${dom%.nc}; "
            "ncdump -v HGT_M,LANDMASK,LU_INDEX \"$f\" 2>/dev/null | "
            "awk -v dom=\"$dom\" '"
            "/^[[:space:]]*HGT_M =/ {m=1} /^[[:space:]]*LANDMASK =/ {m=2} /^[[:space:]]*LU_INDEX =/ {m=3} "
            "m {done=index($0,\";\")>0; gsub(/[=,;]/,\" \"); for(i=1;i<=NF;i++) if($i ~ /^-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$/) {v=$i+0; "
            "if(m==1){n++;s+=v;ss+=v*v;if(n==1||v<mn)mn=v;if(n==1||v>mx)mx=v} "
            "else if(m==2){ln++;ls+=v} else if(m==3){un++;if(v==13)us++}} if(done)m=0} "
            "END {mean=n?s/n:0; variance=n?ss/n-mean*mean:0; if(variance<0)variance=0; "
            "printf \"DOMAIN|%s|%.6f|%.6f|%.6f|%.6f|%.6f|%.6f|%.6f\\n\",dom,mean,mx,sqrt(variance),mx-mn,ln?ls/ln:0,ln?1-ls/ln:0,un?us/un:0}' ; done"
        )
        output = self.run(command, timeout=1800)
        summaries: list[dict[str, Any]] = []
        for line in output.splitlines():
            parts = line.split("|")
            if len(parts) != 9 or parts[0] != "DOMAIN":
                continue
            summaries.append(
                {
                    "domain": parts[1],
                    "terrain_mean_m": float(parts[2]),
                    "terrain_max_m": float(parts[3]),
                    "terrain_std_m": float(parts[4]),
                    "terrain_range_m": float(parts[5]),
                    "land_fraction": float(parts[6]),
                    "water_fraction": float(parts[7]),
                    "urban_fraction": float(parts[8]),
                }
            )
        if len(summaries) != len(domains):
            raise HpcError("geogrid 已运行，但地理统计解析不完整")
        return {"fingerprint": fingerprint, "source": "hpc_wps_geogrid", "domains": summaries, "remote_cache": remote_dir}

    def launch(self, task_id: str, data_source: str = "gfs") -> dict[str, Any]:
        source = "ecmwf" if str(data_source or "gfs").lower() == "ec" else str(data_source or "gfs").lower()
        if source not in {"gfs", "ecmwf"}:
            raise ValueError("数据源必须是 gfs 或 ecmwf")
        expected_index_var = "WRF_GFS_EXPECTED_INDEX" if source == "gfs" else "WRF_EC_EXPECTED_INDEX"
        service_dir, task_dir = self.service_dir(), self.task_dir(task_id)
        runtime = (
            f"WORK_DIR={self.settings.hpc_remote_dir} "
            f"WPS_SOURCE_DIR={self.settings.hpc_wps_source_dir} "
            f"WRF_SOURCE_DIR={self.settings.hpc_wrf_source_dir} "
            f"WRF_CPU_SOURCE_DIR={self.settings.hpc_wrf_source_dir} "
            f"WRF_RUNTIME_ENV={self.settings.hpc_runtime_env} "
            f"WRF_RUNTIME_MIN_FREE_GB={self.settings.hpc_runtime_min_free_gb} "
            f"WRF_RUNTIME_PRE_WRF_MIN_FREE_GB={self.settings.hpc_runtime_pre_wrf_min_free_gb} "
            f"WRF_CPU_MPI_PROCESSES={self.settings.hpc_cpu_mpi_processes} "
            f"GEOG_DATA_PATH={self.settings.hpc_geog_dir} "
            f"WRF_TASK_CONFIG={task_dir}/task.json "
            f"WRF_TASK_ENV={task_dir}/task.env "
            f"{expected_index_var}={task_dir}/forcing.expected.tsv "
            f"WRF_FAILURE_FILE={task_dir}/failure.json "
            f"WRF_NONINTERACTIVE=true "
            f"WRF_GFS_DATA_ROOT={self.settings.hpc_gfs_dir} "
            f"WRF_EC_CACHE_ROOT={self.settings.hpc_ec_dir} "
            f"WRF_EC_GRIB_FILTER={service_dir}/ecmwf_to_wps.filter "
            f"WRF_GFS_MOUNT={self.settings.hpc_gfs_mount} "
        )
        preflight = self.run(
            f"cd {task_dir} || exit 1; {runtime}WRF_PREFLIGHT_ONLY=true "
            f"bash {service_dir}/wrf_hpc_gfs.sh",
            timeout=300,
        )
        runner = (
            f"printf '%s\\n' $$ > {task_dir}/service.pid; "
            f"{runtime}"
            f"bash {service_dir}/wrf_hpc_gfs.sh; "
            f"code=$?; printf '%s\\n' \"$code\" > {task_dir}/exit.code; exit \"$code\""
        )
        command = (
            f"cd {task_dir} || exit 1; "
            f"nohup setsid bash -c {shlex.quote(runner)} "
            f"> {task_dir}/service.log 2>&1 < /dev/null & "
            f"for attempt in 1 2 3 4 5 6 7 8 9 10; do "
            f"test -s {task_dir}/service.pid && break; sleep 0.1; done; "
            f"cat {task_dir}/service.pid"
        )
        pid = self.run(command).splitlines()[-1]
        if not pid.isdigit():
            raise HpcError("未获得远端 WRF 进程 ID")
        return {
            "remote_pid": int(pid),
            "remote_task_dir": task_dir,
            "remote_output_dir": self.remote_output_dir(task_id),
            "preflight": preflight.strip(),
        }

    def status(self, task_id: str) -> dict[str, Any]:
        task_dir = self.task_dir(task_id)
        command = (
            f"if test -f {task_dir}/exit.code; then printf 'EXIT:'; cat {task_dir}/exit.code; "
            f"elif test -f {task_dir}/service.pid && kill -0 $(cat {task_dir}/service.pid) 2>/dev/null; then printf RUNNING; "
            f"elif test -f {task_dir}/service.pid; then printf LOST; else printf MISSING; fi"
        )
        state = self.run(command).strip()
        log = self.run(f"tail -n 80 {task_dir}/service.log 2>/dev/null || true", timeout=60)
        profile_raw = self.run(f"cat {task_dir}/runtime_profile.json 2>/dev/null || true", timeout=60)
        profile: dict[str, Any] = {}
        try:
            parsed_profile = json.loads(profile_raw)
            if isinstance(parsed_profile, dict):
                profile = parsed_profile
        except json.JSONDecodeError:
            pass
        if state.startswith("EXIT:0"):
            return {"status": "succeeded", "log": log, "runtime_profile": profile}
        if state.startswith("EXIT:"):
            failure: dict[str, Any] | None = None
            raw_failure = self.run(f"cat {task_dir}/failure.json 2>/dev/null || true", timeout=60)
            try:
                parsed = json.loads(raw_failure)
                if isinstance(parsed, dict):
                    failure = parsed
            except (TypeError, json.JSONDecodeError):
                failure = None
            return {
                "status": "failed",
                "exit_code": state.split(":", 1)[1].strip(),
                "log": log,
                "failure": failure,
                "runtime_profile": profile,
            }
        if state == "RUNNING":
            return {"status": "running", "log": log, "runtime_profile": profile}
        return {"status": state.lower(), "log": log, "runtime_profile": profile}

    def cleanup_task_attempt(self, task_id: str, *, allow_missing: bool = False) -> dict[str, Any]:
        """清理单个任务尝试；调用方必须先取得用户对精确路径的确认。"""
        state = self.status(task_id)
        remote_status = str(state.get("status") or "unknown")
        if remote_status == "running":
            raise HpcError("远端任务仍在运行，不能清理后重跑")
        if remote_status == "succeeded":
            raise HpcError("远端任务已成功，请恢复结果下载而不是清理重跑")
        if remote_status == "lost":
            raise HpcError("远端进程状态不明，不能自动清理")
        if remote_status == "missing" and not allow_missing:
            raise HpcError("远端任务记录缺失，不能确认清理安全性")
        paths = self.task_artifact_paths(task_id)
        quoted = " ".join(self._quote_remote(path) for path in paths)
        self.run(f"rm -rf -- {quoted}", timeout=300)
        return {"status": remote_status, "deleted_paths": paths}

    def download_outputs(
        self,
        task_id: str,
        local_dir: Path,
        progress: Callable[[int, int], None] | None = None,
        include_names: set[str] | None = None,
    ) -> None:
        self.download_glob(
            f"{self.remote_output_dir(task_id)}/wrfout_d*_*",
            local_dir,
            progress=progress,
            include_names=include_names,
        )

    def validate_outputs(self, task_id: str) -> dict[str, Any]:
        """下载前在超算端确认 wrfout 已稳定且能由 NetCDF/HDF5 打开。"""
        remote_dir = self.remote_output_dir(task_id)
        command = (
            f"cd {self._quote_remote(remote_dir)} || exit 1; "
            # 非交互 SSH 的默认 PATH 会先命中 /usr/local/bin/ncdump；该版本
            # 无法读取 NVHPC WRF 生成的 NetCDF-4。仅在命令替换子 Shell 中
            # 加载 WRF 环境以解析配套工具的绝对路径，环境变量不会外泄。
            f"ncdump_path=$(. {self._quote_remote(self.settings.hpc_runtime_env)} "
            ">/dev/null 2>&1; command -v ncdump 2>/dev/null || true); "
            "test -n \"$ncdump_path\" || ncdump_path=$(command -v ncdump 2>/dev/null || true); "
            "inventory=$(find . -maxdepth 1 -type f -name 'wrfout_d*_*' -printf '%f|%s\\n' | sort); "
            "sleep 2; "
            "printf '%s\\n' \"$inventory\" | "
            "while IFS='|' read -r name size1; do "
            "test -n \"$name\" || continue; "
            "size2=$(wc -c < \"$name\" 2>/dev/null || echo 0); "
            "if test \"$size1\" != \"$size2\"; then "
            "printf 'INVALID|%s|%s|still_writing\\n' \"$name\" \"$size2\"; "
            # tx-lab 的 WRF 环境包含一组可运行模型、但会让 ncdump 将
            # NetCDF-4/HDF5 误报为 Unknown file format 的动态库路径。
            # 仅隔离校验工具，不改变 WPS/WRF 自身的运行环境。
            "elif test -n \"$ncdump_path\" && "
            "! LD_LIBRARY_PATH= \"$ncdump_path\" -h \"$name\" >/dev/null 2>&1; then "
            "printf 'INVALID|%s|%s|netcdf_open_failed\\n' \"$name\" \"$size2\"; "
            "else printf 'VALID|%s|%s|ok\\n' \"$name\" \"$size2\"; fi; done"
        )
        valid: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for line in self.run(command, timeout=900).splitlines():
            parts = line.split("|", 3)
            if len(parts) != 4 or parts[0] not in {"VALID", "INVALID"}:
                continue
            state, name, size_text, reason = parts
            if not SAFE_OUTPUT_NAME.fullmatch(name):
                continue
            item = {"name": name, "size": int(size_text or 0), "reason": reason}
            (valid if state == "VALID" else invalid).append(item)
        if not valid and not invalid:
            raise HpcError("tx-lab 任务成功但没有找到 wrfout 输出")
        return {"valid": valid, "invalid": invalid, "complete": not invalid}

    def cancel(self, task_id: str) -> dict[str, Any]:
        task_dir = self.task_dir(task_id)
        command = (
            f"pid=$(cat {task_dir}/service.pid 2>/dev/null || true); "
            f"case \"$pid\" in ''|*[!0-9]*) printf UNCONFIRMED;; *) "
            f"if kill -0 \"$pid\" 2>/dev/null; then kill -TERM -- -\"$pid\" && printf TERM_SENT; else printf NOT_RUNNING; fi;; esac"
        )
        return {"status": self.run(command).strip().lower()}
