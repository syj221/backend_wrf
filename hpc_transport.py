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
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Callable

from config import Settings


SAFE_TASK_ID = re.compile(r"^wrf_(?:gfs_)?[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
SAFE_CYCLE = re.compile(r"^[0-9]{10}$")
SAFE_REMOTE_ROOT = re.compile(r"^(?:~|/)[A-Za-z0-9_./-]*$")
SAFE_GFS_NAME = re.compile(r"^gfs\.t[0-9]{2}z\.pgrb2\.0p25\.f([0-9]{3})$")
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9_.*?+:-]+$")
SAFE_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MEBIBYTE = 1024 * 1024
GFS_PRODUCT = "gfs-0p25-full"
GFS_SCOPE = "full_file"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class HpcError(RuntimeError):
    pass


class HpcAuthError(HpcError):
    pass


class HpcSessionStaleError(HpcError):
    """命令尚未下发，但持久会话已经不在计算节点 Shell。"""


class HpcClient:
    """通过堡垒机维护一个可复用的计算节点 TTY 会话。"""

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
        for label, value in (
            ("WRF_HPC_REMOTE_DIR", settings.hpc_remote_dir),
            ("WRF_HPC_GFS_DIR", settings.hpc_gfs_dir),
            ("WRF_HPC_WPS_SOURCE_DIR", settings.hpc_wps_source_dir),
            ("WRF_HPC_WRF_SOURCE_DIR", settings.hpc_wrf_source_dir),
            ("WRF_HPC_GEOG_DATA_PATH", settings.hpc_geog_dir),
            ("WRF_HPC_GFS_DOWNLOAD_SCRIPT", settings.hpc_gfs_download_script),
        ):
            if not SAFE_REMOTE_ROOT.fullmatch(value):
                raise ValueError(f"{label} 包含不安全字符")

    @property
    def target(self) -> str:
        if self.settings.hpc_user:
            return f"{self.settings.hpc_user}@{self.settings.hpc_host}"
        return self.settings.hpc_host

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
            "ssh",
            "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=no",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
        ]
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
            "scp", "-q",
            "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
        ]
        if self.settings.hpc_auth_mode == "key":
            command.extend(["-o", "BatchMode=yes"])
        if self.settings.hpc_key_file:
            command.extend(["-i", self.settings.hpc_key_file])
        return command

    def _sftp_base(self) -> list[str]:
        command = [
            "sftp",
            "-o", f"ConnectTimeout={self.settings.hpc_connect_timeout}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=no",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
        ]
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
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise HpcError("超算命令执行超时") from exc
        if result.returncode:
            message = (result.stderr or result.stdout or "超算命令失败").strip()
            if "permission denied" in message.lower():
                raise HpcAuthError("需要输入超算登录密码")
            raise HpcError(message)
        return result

    def authenticate_password(self, password: str) -> dict[str, Any]:
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
            raise HpcError(f"超算输出不存在：{posixpath.basename(remote_path)}")
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
                raise HpcError("超算任务没有可下载的有效 wrfout 输出")
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
                local_dir / name,
                timeout,
                expected_meta=(size, digest),
                progress=file_progress,
            )
            completed_size += size

    def health(self) -> dict[str, Any]:
        try:
            output = self.run(
                f"missing=''; test -d {self.settings.hpc_wps_source_dir} || missing=\"$missing WPS\"; "
                f"test -d {self.settings.hpc_wrf_source_dir} || missing=\"$missing WRF\"; "
                f"test -d {self.settings.hpc_geog_dir} || missing=\"$missing WPS_GEOG\"; "
                "if test -z \"$missing\"; then printf WRF_HPC_READY; else printf '缺少:%s' \"$missing\"; fi",
                timeout=self.settings.hpc_connect_timeout + 10,
            )
            ready = "WRF_HPC_READY" in output
            return {
                "status": "ready" if ready else "unavailable",
                "message": output,
                "connection_mode": self.settings.hpc_connection_mode,
                "transfer": self.transfer_status,
                **self.connection_diagnostic,
            }
        except HpcAuthError:
            return {
                "status": "auth_required",
                "message": "需要输入超算登录密码",
                "connection_mode": self.settings.hpc_connection_mode,
                "transfer": self.transfer_status,
                **self.connection_diagnostic,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "message": str(exc),
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

    def prepare_runtime(
        self,
        task_id: str,
        config_path: Path,
        environment_path: Path,
        expected_gfs_path: Path,
        script_dir: Path,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        service_dir, task_dir = self.service_dir(), self.task_dir(task_id)
        self.run(f"mkdir -p {service_dir} {task_dir}")
        items = [
            (script_dir / "wrf.sh", f"{service_dir}/wrf.sh"),
            (script_dir / "wrf_hpc_gfs.sh", f"{service_dir}/wrf_hpc_gfs.sh"),
            (config_path, f"{task_dir}/task.json"),
            (environment_path, f"{task_dir}/task.env"),
            (expected_gfs_path, f"{task_dir}/gfs.expected.tsv"),
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
            f"chmod 700 {service_dir}/wrf.sh {service_dir}/wrf_hpc_gfs.sh; "
            f"chmod 600 {task_dir}/task.json {task_dir}/task.env {task_dir}/gfs.expected.tsv"
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
        full_manifest = (
            isinstance(manifest, dict)
            and manifest.get("cycle") == cycle
            and manifest.get("product") == GFS_PRODUCT
            and manifest.get("scope") == GFS_SCOPE
        )
        if full_manifest:
            for item in manifest.get("files", []):
                entry = self._manifest_entry(item, cycle)
                if entry is not None:
                    candidates[entry["name"]] = entry

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

        if valid_entries:
            merged = dict(candidates)
            merged.update({item["name"]: item for item in valid_entries})
            files_meta = sorted(merged.values(), key=lambda item: item["forecast_hour"])
            available = {item["forecast_hour"] for item in files_meta}
            rebuilt = {
                "provider": "gfs",
                "product": GFS_PRODUCT,
                "scope": GFS_SCOPE,
                "cycle": cycle,
                "forecast_hours": sorted(available),
                "complete": all(hour in available for hour in range(73)),
                "files": files_meta,
                "updated_at": _utc_now(),
                "managed_by": "backend_wrf_remote_pool",
            }
            manifest_matches = (
                full_manifest
                and manifest.get("files") == rebuilt["files"]
                and bool(manifest.get("complete")) == rebuilt["complete"]
            )
            if not manifest_matches:
                self._upload_text_atomic(
                    json.dumps(rebuilt, ensure_ascii=False, indent=2), manifest_path
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
            "legacy_imported_hours": [],
            "manifest_needs_rebuild": bool(unhashed),
            "manifest_is_full": full_manifest,
        }

    def trigger_gfs_download(self, cycle: str, horizon: int = 72) -> dict[str, Any]:
        """幂等触发超算共享 GFS 下载；已有同周期进程时只返回其状态。"""
        self._assert_cycle(cycle)
        if cycle[8:10] != "00":
            raise ValueError("当前 WRF 数据池仅允许触发 00Z 周期")
        horizon = max(0, min(72, int(horizon)))
        cycle_date = cycle[:8]
        script = self._quote_remote(self.settings.hpc_gfs_download_script)
        log_dir = f"{self.settings.hpc_gfs_dir}/logs"
        remote_dir = f"{self.settings.hpc_gfs_dir}/{cycle}"
        log_path = f"{log_dir}/download_{cycle}.out"
        pattern = shlex.quote(f"[d]ownload_gfs_00z.sh {cycle_date}")
        command = (
            f"mkdir -p {self._quote_remote(log_dir)}; "
            f"if ! test -x {script}; then printf 'ERROR|download_script_not_executable'; "
            f"elif pgrep -f {pattern} >/dev/null 2>&1; then printf 'RUNNING|shared'; "
            "else "
            f"nohup bash {script} {cycle_date} "
            f"> {self._quote_remote(log_path)} 2>&1 < /dev/null & pid=$!; sleep 2; "
            "if kill -0 \"$pid\" >/dev/null 2>&1; then printf 'STARTED|%s' \"$pid\"; "
            f"elif test \"$(find {self._quote_remote(remote_dir)} -maxdepth 1 -type f "
            "-name 'gfs.t00z.pgrb2.0p25.f???' 2>/dev/null | wc -l)\" -ge 73; "
            "then printf 'READY|73/73'; else "
            f"message=$(tail -n 1 {self._quote_remote(log_path)} 2>/dev/null "
            "| tr '|\r\n' '   ' | cut -c1-240); "
            "printf 'FAILED|%s' \"${message:-download process exited immediately}\"; fi; fi"
        )
        state, _separator, detail = self.run(command, timeout=60).partition("|")
        if state == "ERROR":
            raise HpcError("超算 GFS 下载脚本不存在或不可执行")
        return {"cycle": cycle, "status": state.lower(), "detail": detail}

    def ensure_remote_gfs(
        self,
        cycle: str,
        forecast_hours: list[int],
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """仅在超算数据池检查/触发下载；取消等待不会杀死共享下载进程。"""
        deadline = time.monotonic() + self.settings.hpc_gfs_wait_seconds
        result = self.inspect_gfs_files(cycle, forecast_hours)
        if result["complete"]:
            return result
        trigger = self.trigger_gfs_download(cycle, 72)
        if progress:
            progress({**result, "download": trigger})
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                raise HpcError("任务已取消等待；超算共享 GFS 下载继续运行")
            time.sleep(self.settings.hpc_gfs_poll_seconds)
            result = self.inspect_gfs_files(cycle, forecast_hours)
            if progress:
                progress({**result, "download": trigger})
            if result["complete"]:
                return result
        raise HpcError(f"等待超算 GFS {cycle} 数据池就绪超时（{self.settings.hpc_gfs_wait_seconds // 60} 分钟）")

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
        target_feed = ""
        if targets:
            target_feed = "printf '%s\\n' " + " ".join(shlex.quote(cycle) for cycle in sorted(targets)) + "; "
        output = self.run(
            f"cd {self._quote_remote(root)} 2>/dev/null || exit 0; "
            "root=$(pwd -P); printf 'ROOT|%s\\n' \"$root\"; "
            f"{{ find . -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'; {target_feed}}} | "
            "awk '/^[0-9]{10}$/' | sort -r -u | while read cycle; do "
            "dir=\"$cycle\"; count=$(find \"$dir\" -maxdepth 1 -type f -name 'gfs.t00z.pgrb2.0p25.f???' 2>/dev/null | wc -l); "
            "size=$(find \"$dir\" -maxdepth 1 -type f -name 'gfs.t00z.pgrb2.0p25.f???' -printf '%s\\n' 2>/dev/null | awk '{s+=$1} END {print s+0}'); "
            "last=$(find \"$dir\" -maxdepth 1 -type f -name 'gfs.t00z.pgrb2.0p25.f???' -printf '%f\\n' 2>/dev/null | sed -n 's/.*f\\([0-9][0-9][0-9]\\)$/\\1/p' | sort -n | tail -1); "
            "complete=1; hour=0; while test \"$hour\" -le 72; do fh=$(printf '%03d' \"$hour\"); "
            "test -s \"$dir/gfs.t00z.pgrb2.0p25.f$fh\" || complete=0; hour=$((hour + 1)); done; "
            "log=\"logs/download_${cycle}.out\"; message=''; "
            "test -s \"$log\" && message=$(tail -n 1 \"$log\" | tr '|\\r\\n' '   ' | cut -c1-240); "
            "cycle_date=${cycle%00}; if pgrep -f \"[d]ownload_gfs_00z.sh $cycle_date\" >/dev/null 2>&1; then state=downloading; "
            "elif test \"$complete\" -eq 1; then state=ready; else state=partial; fi; "
            "if test \"$state\" = partial && test -n \"$message\"; then state=error; "
            "elif test \"$state\" = partial && test ! -d \"$dir\"; then state=missing; fi; "
            "printf '%s|%s|%s|%s|%s|%s\\n' \"$cycle\" \"$state\" \"$count\" \"$size\" \"${last:-0}\" \"$message\"; done",
            timeout=120,
        )
        remote_root = ""
        cycles: list[dict[str, Any]] = []
        for line in output.splitlines():
            if line.startswith("ROOT|"):
                remote_root = line.partition("|")[2].strip().rstrip("/")
                continue
            parts = line.split("|", 5)
            if len(parts) not in {5, 6} or not SAFE_CYCLE.fullmatch(parts[0]):
                continue
            cycle, status, count, size, last = parts[:5]
            download_message = parts[5].strip() if len(parts) == 6 else ""
            cycles.append(
                {
                    "cycle": cycle,
                    "status": status,
                    "forecast_start": 0 if int(count) else None,
                    "forecast_end": int(last) if int(count) else None,
                    "completed_files": int(count),
                    "total_files": 73,
                    "size_bytes": int(size),
                    "complete": status == "ready",
                    "remote_path": f"{remote_root}/{cycle}" if remote_root else "",
                    "target": cycle in targets,
                    "protected": cycle in protected,
                    "download_message": download_message,
                }
            )
        by_cycle = {item["cycle"]: item for item in cycles}
        targets_complete = bool(targets) and all(by_cycle.get(cycle, {}).get("complete") for cycle in targets)
        for item in cycles:
            item["cleanup_allowed"] = bool(
                targets_complete
                and not item["target"]
                and not item["protected"]
                and item["status"] != "downloading"
                and item["remote_path"]
            )
        if any(item["status"] == "downloading" for item in cycles):
            overall = "downloading"
        elif any(item["status"] == "error" and item["target"] for item in cycles):
            overall = "error"
        elif targets_complete:
            overall = "ready"
        elif cycles:
            overall = "partial"
        else:
            overall = "idle"
        cleanup_candidates = [item["remote_path"] for item in cycles if item["cleanup_allowed"]]
        return [{
            "provider": "gfs",
            "label": "GFS",
            "source": "NOAA NOMADS（超算直连）",
            "status": overall,
            "remote_root": remote_root,
            "target_cycles": list(target_cycles or []),
            "targets_complete": targets_complete,
            "cleanup_candidates": cleanup_candidates,
            "cycles": cycles,
        }]

    def cleanup_gfs_cycles(
        self,
        paths: list[str],
        target_cycles: list[str],
        protected_cycles: set[str],
    ) -> dict[str, Any]:
        """只删除数据池当前明确标记为可清理的精确周期路径。"""
        requested = list(dict.fromkeys(str(path).rstrip("/") for path in paths))
        if not requested:
            raise ValueError("至少选择一个待清理的超算 GFS 周期")
        pool = self.gfs_pool_items(target_cycles, protected_cycles)[0]
        allowed = set(pool.get("cleanup_candidates") or [])
        refused = [path for path in requested if path not in allowed]
        if refused:
            raise ValueError(f"以下路径当前不允许清理：{', '.join(refused)}")
        root = str(pool.get("remote_root") or "").rstrip("/")
        if not root:
            raise HpcError("无法确认超算 GFS 数据池绝对路径")
        commands: list[str] = []
        for path in requested:
            cycle = posixpath.basename(path)
            if not SAFE_CYCLE.fullmatch(cycle) or posixpath.dirname(path) != root:
                raise ValueError(f"拒绝清理非受管 GFS 周期目录：{path}")
            quoted_path = shlex.quote(path)
            pattern = shlex.quote(f"[d]ownload_gfs_00z.sh {cycle[:8]}")
            commands.append(
                f"if pgrep -f {pattern} >/dev/null 2>&1; then "
                f"printf 'BLOCKED|%s\\n' {quoted_path}; "
                f"elif test -d {quoted_path}; then rm -rf -- {quoted_path} && printf 'DELETED|%s\\n' {quoted_path}; "
                f"else printf 'MISSING|%s\\n' {quoted_path}; fi"
            )
        output = self.run("; ".join(commands), timeout=120)
        deleted: list[str] = []
        missing: list[str] = []
        blocked: list[str] = []
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
        if blocked:
            raise HpcError(f"周期下载进程仍在运行，未清理：{', '.join(blocked)}")
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
        module_setup = (
            "if ! command -v module >/dev/null 2>&1; then "
            "test -r /etc/profile.d/modules.sh && . /etc/profile.d/modules.sh; "
            "test -r /usr/share/Modules/init/bash && . /usr/share/Modules/init/bash; fi; "
            "if command -v module >/dev/null 2>&1; then module purge >/dev/null 2>&1; "
            "module load intel hdf5 netcdf jasper libpng zlib openmpi/4.1.4 >/dev/null 2>&1 || exit 21; fi; "
        )
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

    def launch(self, task_id: str) -> dict[str, Any]:
        service_dir, task_dir = self.service_dir(), self.task_dir(task_id)
        runtime = (
            f"WORK_DIR={self.settings.hpc_remote_dir} "
            f"WPS_SOURCE_DIR={self.settings.hpc_wps_source_dir} "
            f"WRF_SOURCE_DIR={self.settings.hpc_wrf_source_dir} "
            f"GEOG_DATA_PATH={self.settings.hpc_geog_dir} "
            f"WRF_TASK_CONFIG={task_dir}/task.json "
            f"WRF_TASK_ENV={task_dir}/task.env "
            f"WRF_GFS_EXPECTED_INDEX={task_dir}/gfs.expected.tsv "
            f"WRF_NONINTERACTIVE=true "
            f"WRF_GFS_DATA_ROOT={self.settings.hpc_gfs_dir} "
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
        if state.startswith("EXIT:0"):
            return {"status": "succeeded", "log": log}
        if state.startswith("EXIT:"):
            return {"status": "failed", "exit_code": state.split(":", 1)[1].strip(), "log": log}
        if state == "RUNNING":
            return {"status": "running", "log": log}
        return {"status": state.lower(), "log": log}

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
            "find . -maxdepth 1 -type f -name 'wrfout_d*_*' -printf '%f|%s\\n' | sort > .wrfout_sizes_1; "
            "sleep 2; "
            "while IFS='|' read -r name size1; do "
            "size2=$(wc -c < \"$name\" 2>/dev/null || echo 0); "
            "if test \"$size1\" != \"$size2\"; then "
            "printf 'INVALID|%s|%s|still_writing\\n' \"$name\" \"$size2\"; "
            "elif command -v ncdump >/dev/null 2>&1 && ! ncdump -h \"$name\" >/dev/null 2>&1; then "
            "printf 'INVALID|%s|%s|netcdf_open_failed\\n' \"$name\" \"$size2\"; "
            "else printf 'VALID|%s|%s|ok\\n' \"$name\" \"$size2\"; fi; done < .wrfout_sizes_1; "
            "rm -f .wrfout_sizes_1"
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
            raise HpcError("超算任务成功但没有找到 wrfout 输出")
        return {"valid": valid, "invalid": invalid, "complete": not invalid}

    def cancel(self, task_id: str) -> dict[str, Any]:
        task_dir = self.task_dir(task_id)
        command = (
            f"pid=$(cat {task_dir}/service.pid 2>/dev/null || true); "
            f"case \"$pid\" in ''|*[!0-9]*) printf UNCONFIRMED;; *) "
            f"if kill -0 \"$pid\" 2>/dev/null; then kill -TERM -- -\"$pid\" && printf TERM_SENT; else printf NOT_RUNNING; fi;; esac"
        )
        return {"status": self.run(command).strip().lower()}
