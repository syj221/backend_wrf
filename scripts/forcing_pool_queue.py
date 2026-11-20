#!/usr/bin/env python3
"""Single-worker, persistent download queue for a WRF forecast data pool.

The program is deliberately dependency-free because it is copied to tx-lab
beside the two small shell entry points.  A call to ``enqueue`` is quick and
idempotent; the detached ``worker`` owns the source-specific worker lock and
downloads one file at a time.  State lives below the provider data root, so a
service restart never loses partial files or the work order.
"""

import argparse
import email.utils
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import shutil
import stat
import socket
from urllib.parse import urlsplit
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CYCLE_RE = re.compile(r"^\d{8}00$")
HOURS = tuple(range(73))
ECMWF_HOURS = tuple(range(0, 73, 3))
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
TRANSIENT_CURL_ERRORS = {6, 7, 18, 28, 35, 52, 56}
LOCK_ROOT = Path("/tmp") / f"wrf-forcing-locks-{os.getuid()}"
ECMWF_PORTAL = "https://data.ecmwf.int/forecasts"
ECMWF_CLOUDFRONT = "https://d2zvc0wgha4k2l.cloudfront.net"
NOAA_NOMADS = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
NOAA_AWS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_now() -> int:
    return int(time.time())


def safe_cycle(value: str) -> str:
    if not CYCLE_RE.fullmatch(value):
        raise ValueError("cycle must be YYYYMMDD00")
    return value


def parse_hours(value: str) -> List[int]:
    items = sorted({int(item) for item in value.split(",") if item.strip()})
    if not items or items[0] < 0 or items[-1] > 72:
        raise ValueError("forecast hours must be within f000-f072")
    return items


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
        json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        temporary = Path(output.name)
    os.replace(temporary, path)


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@contextmanager
def kernel_guard(path: Path, blocking: bool = True):
    """Keep advisory locks on the host filesystem, not the FUSE data volume."""
    root = LOCK_ROOT
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("unsafe forcing lock directory")
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    fd = os.open(root / key, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("unsafe forcing lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
            return
        yield fd
    finally:
        # Do not LOCK_UN: a surviving curl child inherits this descriptor and
        # must keep the lock until it has stopped writing the partial file.
        os.close(fd)


@contextmanager
def exclusive(path: Path, blocking: bool = True):
    with kernel_guard(path, blocking) as guard:
        if guard is None:
            yield None
            return
        with legacy_exclusive(path, blocking) as handle:
            yield handle


@contextmanager
def legacy_exclusive(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            yield None
            return
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_process(path: Path):
    """Use an inherited kernel lock plus the legacy worker presence marker.

    An incomplete or legacy marker is unknown, even if its PID is absent.
    Only a v2 marker protected by this same kernel gate can be reclaimed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with kernel_guard(path, blocking=False) as guard:
        if guard is None:
            yield None
            return
        if path.exists() or path.is_symlink():
            owner = read_json(path / "owner.json") if not path.is_symlink() else {}
            if owner.get("protocol") != "wrf-pool-worker-2" or owner.get("root") != str(path.resolve()):
                yield None
                return
            if {item.name for item in path.iterdir()} != {"pid", "owner.json"}:
                yield None
                return
            # The free inherited kernel lock proves no v2 worker or curl
            # still owns this marker, independent of PID reuse or reboot.
            (path / "owner.json").unlink()
            (path / "pid").unlink()
            path.rmdir()
        path.mkdir()
        marker = path.stat().st_ino
        try:
            (path / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
            atomic_json(path / "owner.json", {"protocol": "wrf-pool-worker-2", "pid": os.getpid(),
                        "root": str(path.resolve()), "created_at": utc_now()})
            yield guard
        finally:
            if path.is_dir() and not path.is_symlink() and path.stat().st_ino == marker:
                for name in ("owner.json", "pid"):
                    try:
                        (path / name).unlink()
                    except FileNotFoundError:
                        pass
                path.rmdir()


class PoolQueue:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.source = args.source
        self.root = Path(args.root).resolve()
        self.state_path = self.root / ".pool-queue.json"
        self.state_lock = self.root / ".pool-queue.lock"
        self.worker_lock = self.root / ".pool-worker.lockdir"
        self.worker_fds: Tuple[int, ...] = ()

    def expected_hours(self) -> Tuple[int, ...]:
        """Return the forecast fields actually published by this provider."""
        return HOURS if self.source == "gfs" else ECMWF_HOURS

    def file_name(self, hour: int) -> str:
        if self.source == "gfs":
            return f"gfs.t00z.pgrb2.0p25.f{hour:03d}"
        return f"ecmwf.t00z.ifs.0p25.f{hour:03d}.grib2"

    def file_path(self, cycle: str, hour: int) -> Path:
        return self.root / cycle / self.file_name(hour)

    def cycle_lock(self, cycle: str) -> Path:
        return self.root / f".pool-cycle-{safe_cycle(cycle)}.lock"

    def default_state(self) -> Dict[str, Any]:
        return {"version": 1, "source": self.source, "updated_at": utc_now(), "cycles": {}}

    def load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return self.default_state()
        state = read_json(self.state_path)
        if state.get("version") != 1 or state.get("source") != self.source or not isinstance(state.get("cycles"), dict):
            raise ValueError("forcing queue state is unreadable or invalid; preserve it for operator review")
        return state

    def snapshot(self, cycle: str) -> Dict[str, Any]:
        """Fingerprint exact cycle contents without following symbolic links."""
        cycle = safe_cycle(cycle)
        root = self.root / cycle
        entries, size, files = [], 0, 0
        def visit(path):
            nonlocal size, files
            before = path.lstat()
            item = {"path":str(path.relative_to(root)),"device":before.st_dev,
                    "inode":before.st_ino,"size":before.st_size,"mtime_ns":before.st_mtime_ns}
            if stat.S_ISLNK(before.st_mode) or not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
                raise ValueError("cycle contains a symbolic link or unsupported entry")
            if stat.S_ISDIR(before.st_mode):
                item["kind"] = "directory"
                for child in sorted(path.iterdir()):
                    visit(child)
            else:
                item["kind"] = "file"
                descriptor = os.open(str(path),os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor,"rb") as stream:
                    if (os.fstat(stream.fileno()).st_dev,os.fstat(stream.fileno()).st_ino) != (before.st_dev,before.st_ino):
                        raise ValueError("cycle entry changed while reading")
                    digest = hashlib.sha256()
                    for block in iter(lambda:stream.read(1024*1024),b""):
                        digest.update(block)
                    item["sha256"] = digest.hexdigest()
                size += before.st_size
                files += 1
            after = path.lstat()
            if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
                raise ValueError("cycle changed while fingerprinting")
            entries.append(item)
        if root.exists() or root.is_symlink():
            visit(root)
        value = {"path":str(root),"cycle":cycle,"exists":bool(entries),"entries":entries}
        return {"path":str(root),"cycle":cycle,"files":files,"bytes":size,
                "digest":hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()}

    def cleanup(self, cycle: str, expected_digest=None) -> str:
        """Delete one caller-approved cycle while no source worker can write."""
        cycle = safe_cycle(cycle)
        path = self.root / cycle
        with exclusive_process(self.worker_lock) as worker:
            if worker is None:
                return "BLOCKED"
            with exclusive(self.state_lock):
                state = self.load_state()
                if path.is_symlink() or (path.exists() and not path.is_dir()):
                    return "BLOCKED"
                entry = state.get("cycles", {}).get(cycle) or {}
                if any(item.get("priority") == "task" and item.get("status") not in {"ready", "failed"}
                       for item in (entry.get("files") or {}).values()):
                    return "BLOCKED"
                if expected_digest and self.snapshot(cycle)["digest"] != expected_digest:
                    return "BLOCKED"
                # Preserve the queue entry in a durable intent before removal.
                # A lost SSH response is reconciled from this audit, not retried.
                audit = self.root / "logs" / f"cleanup_{cycle}_{uuid.uuid4().hex}.json"
                record = {"version": 1, "source": self.source, "cycle": cycle,
                          "path": str(path), "queue_entry": entry, "status": "started", "created_at": utc_now()}
                atomic_json(audit, record)
                existed = path.exists()
                if existed:
                    shutil.rmtree(path)
                if path.exists() or path.is_symlink():
                    raise RuntimeError("cycle still exists after cleanup")
                state.get("cycles", {}).pop(cycle, None)
                self.save_state(state)
                outcome = "DELETED" if existed else "MISSING"
                atomic_json(audit, {**record, "status": outcome.lower(), "finished_at": utc_now()})
                return outcome

    def save_state(self, state: Dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_json(self.state_path, state)

    def classify_error(self, status: int, error: str) -> Tuple[str, bool]:
        value = str(error or "").lower()
        if any(token in value for token in ("syntaxerror", "future feature", "module not found", "not executable", "command not found", "no such file")):
            return "runtime_compatibility", False
        if any(token in value for token in ("permission denied", "read-only", "no space left", "storage", "mount")):
            return "storage", False
        if any(token in value for token in ("grib", "checksum", "invalid", "corrupt", "7777")):
            return "data_integrity", False
        if status == 0 or any(
            token in value
            for token in (
                "could not resolve",
                "temporary failure",
                "timed out",
                "timeout",
                "connection reset",
                "connection refused",
                "connection closed",
            )
        ):
            return "network", True
        if status in RETRYABLE_HTTP or any(f"curl: ({code})" in value for code in TRANSIENT_CURL_ERRORS):
            return "upstream_temporary", True
        if status >= 400:
            return "upstream_permanent", False
        return "unknown", False

    def valid(self, path: Path) -> bool:
        try:
            if path.stat().st_size < self.args.min_bytes:
                return False
            with path.open("rb") as input_file:
                if input_file.read(4) != b"GRIB":
                    return False
                input_file.seek(-4, os.SEEK_END)
                return input_file.read(4) == b"7777"
        except OSError:
            return False

    @staticmethod
    def digest(path: Path) -> str:
        value = hashlib.sha256()
        with path.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                value.update(chunk)
        return value.hexdigest()

    def part_size(self, cycle: str, hour: int) -> int:
        try:
            return self.file_path(cycle, hour).with_suffix(self.file_path(cycle, hour).suffix + ".part").stat().st_size
        except OSError:
            return 0

    def url(self, cycle: str, hour: int) -> str:
        date, init = cycle[:8], cycle[8:]
        if self.source == "gfs":
            return (
                f"{NOAA_NOMADS}/"
                f"gfs.{date}/{init}/atmos/gfs.t{init}z.pgrb2.0p25.f{hour:03d}"
            )
        template = self.args.ec_template
        if template:
            return (template.replace("{cycle}", cycle).replace("{fh}", f"{hour:03d}")
                    .replace("{date}", date).replace("{hour}", init).replace("{step}", str(hour)))
        return f"{ECMWF_PORTAL}/{date}/{init}z/ifs/0p25/oper/{date}{init}0000-{hour}h-oper-fc.grib2"

    def transfer_routes(self, cycle, hour):
        """Return the legacy route and the ordered routes for a fresh object."""
        original = self.url(cycle, hour)
        if self.source == "gfs":
            return "nomads", [("nomads", original), ("noaa_aws", NOAA_AWS + original[len(NOAA_NOMADS):])]
        if self.args.ec_template:
            return "custom", [("custom", original)]
        return "ecmwf_portal", [
            ("ecmwf_cloudfront", ECMWF_CLOUDFRONT + original[len(ECMWF_PORTAL):]),
            ("ecmwf_portal", original),
        ]

    def transfer_file(self, cycle, hour, command, entry, before, **kwargs):
        legacy, routes = self.transfer_routes(cycle, hour)
        if before:
            origin = entry.get("transfer_node") or legacy
            pin = entry.get("transfer_url_sha256")
            routes = [(node, url) for node, url in routes if node == origin
                      and (not pin or hashlib.sha256(url.encode()).hexdigest() == pin)]
            if not routes:
                raise OSError("Partial transfer origin no longer matches its configured source")
        deadline = time.monotonic() + float(command[command.index("--max-time") + 1])
        output = Path(command[command.index("--output") + 1])
        headers = Path(command[command.index("--dump-header") + 1])
        result = None
        for node, url in routes:
            remaining = deadline - time.monotonic()
            if result is not None and remaining < 1:
                break
            attempt = list(command)
            attempt[-1] = url
            attempt[attempt.index("--max-time") + 1] = str(max(1, int(remaining)))
            attempt[attempt.index("--connect-timeout") + 1] = str(min(30, max(1, int(remaining))))
            # Persist the origin before starting curl, including after a zero-byte failover.
            self._set_entry(cycle, hour, {"status": "downloading", "transfer_node": node,
                "transfer_url_sha256": hashlib.sha256(url.encode()).hexdigest()})
            self._attempt_context["transfer_node"] = node
            try:
                headers.unlink()
            except FileNotFoundError:
                pass
            result = self.run_curl(attempt, **kwargs)
            status = self.response_status(headers, result)
            if before or (output.exists() and output.stat().st_size):
                break
            if not (result.returncode in TRANSIENT_CURL_ERRORS
                    or result.returncode == 22 and status in {403, 404, 408, 425, 500, 502, 503, 504}):
                break
        return result

    def enqueue(self, cycle: str, hours: List[int], priority: str, reference=None) -> None:
        cycle = safe_cycle(cycle)
        if reference and not re.fullmatch(r"forcing_[a-f0-9]{32}",reference):
            raise ValueError("invalid download reference")
        if self.source in {"ecmwf", "ecmwf_0p1"}:
            invalid = sorted(set(hours) - set(self.expected_hours()))
            if invalid:
                raise ValueError("ECMWF Open Data 仅提供 3 小时时次：" + ",".join(f"f{hour:03d}" for hour in invalid))
        with exclusive(self.state_lock) as lock:
            if lock is None:
                raise RuntimeError("could not acquire queue state lock")
            state = self.load_state()
            item = state["cycles"].setdefault(cycle, {"files": {}, "publication": "unknown"})
            files = item.setdefault("files", {})
            expected = {f"{hour:03d}" for hour in self.expected_hours()}
            # Older queue versions incorrectly created hourly ECMWF entries.
            # Remove them from scheduling so an unavailable 1-hour field can
            # never hold a valid 3-hour cycle in the failed state.
            if self.source in {"ecmwf", "ecmwf_0p1"}:
                for key, entry in files.items():
                    if key not in expected and isinstance(entry, dict):
                        entry.update({
                            "status": "obsolete",
                            "ignored_reason": "ECMWF Open Data publishes 3-hour fields only",
                            "retry_at": 0,
                        })
            for hour in hours:
                key = f"{hour:03d}"
                entry = files.setdefault(key, {"status": "queued", "priority": priority, "attempts": 0, "no_progress_attempts": 0})
                if reference:
                    entry.setdefault("references",{})[reference] = True
                elif priority == "task":
                    entry["legacy_task_reference"] = True
                if priority == "prefetch":
                    entry["prefetch_reference"] = True
                if entry.get("status") == "cancelled":
                    entry["status"] = "queued"
                path = self.file_path(cycle, hour)
                if self.valid(path):
                    size = path.stat().st_size
                    if int(entry.get("bytes") or 0) != size or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256") or "")):
                        entry["sha256"] = self.digest(path)
                    entry.update({"status": "ready", "priority": priority if priority == "task" else entry.get("priority", priority), "bytes": size, "terminal_retry_at": 0, "failure_class": "", "error": None, "failed_at": 0, "retryable": False})
                elif priority == "task":
                    entry["priority"] = "task"
                    if entry.get("status") in {"failed", "retry_wait"}:
                        entry.update({"status": "queued", "attempts": 0, "no_progress_attempts": 0, "error": None, "retry_at": 0, "terminal_retry_at": 0, "failure_class": "", "failed_at": 0, "retryable": False})
                else:
                    entry.setdefault("priority", "prefetch")
                    if (
                        entry.get("status") in {"failed", "retry_wait"}
                        and (bool(entry.get("retryable")) or not entry.get("http_status"))
                        and int(entry.get("terminal_retry_at") or 0) <= epoch_now()
                    ):
                        entry.update({"status": "queued", "attempts": 0, "no_progress_attempts": 0, "error": None, "retry_at": 0, "terminal_retry_at": 0, "failure_class": "", "failed_at": 0, "retryable": False})
            item["updated_at"] = utc_now()
            self.save_state(state)

    def release_reference(self, cycle, reference):
        cycle = safe_cycle(cycle)
        if not re.fullmatch(r"forcing_[a-f0-9]{32}",reference or ""):
            raise ValueError("invalid download reference")
        with exclusive(self.state_lock):
            state = self.load_state()
            for entry in state.get("cycles",{}).get(cycle,{}).get("files",{}).values():
                # An old untracked task intent is preserved conservatively.
                refs = entry.get("references")
                if not isinstance(refs,dict) or reference not in refs:
                    continue
                refs.pop(reference)
                if not refs and not entry.get("legacy_task_reference"):
                    entry["priority"] = "prefetch"
                    if not entry.get("prefetch_reference") and entry.get("status") not in {"ready","downloading"}:
                        entry["status"] = "cancelled"
            self.save_state(state)

    def expire_prefetch(self, keep):
        keep = {safe_cycle(cycle) for cycle in keep}
        with exclusive(self.state_lock) as lock:
            if lock is None:
                raise RuntimeError("could not acquire queue state lock")
            state = self.load_state()
            for cycle,item in state.get("cycles",{}).items():
                if cycle in keep:
                    continue
                for entry in item.get("files",{}).values():
                    entry["prefetch_reference"] = False
                    if (not entry.get("references") and not entry.get("legacy_task_reference")
                            and entry.get("status") not in {"ready","downloading"}):
                        entry.update(status="cancelled",retry_at=0)
                if not any(entry.get("references") or entry.get("legacy_task_reference") or entry.get("status")=="downloading" for entry in item.get("files",{}).values()):
                    item.update(publication="expired",publication_error="",active_file=None)
            self.save_state(state)

    def forget(self, cycle: str) -> None:
        """Remove queue metadata after the managed cycle directory is gone."""
        cycle = safe_cycle(cycle)
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return
            state = self.load_state()
            if cycle in state.get("cycles", {}):
                state["cycles"].pop(cycle, None)
                self.save_state(state)

    def _set_entry(self, cycle: str, hour: int, values: Dict[str, Any]) -> None:
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return
            state = self.load_state()
            cycle_state = state.setdefault("cycles", {}).setdefault(cycle, {"files": {}, "publication": "unknown"})
            entry = cycle_state.setdefault("files", {}).setdefault(f"{hour:03d}", {"status": "queued", "priority": "prefetch", "attempts": 0, "no_progress_attempts": 0})
            entry.update(values)
            entry["updated_at"] = utc_now()
            cycle_state["updated_at"] = utc_now()
            if values.get("status") == "downloading":
                cycle_state["active_file"] = self.file_name(hour)
            elif cycle_state.get("active_file") == self.file_name(hour):
                cycle_state["active_file"] = None
            self.save_state(state)

    def _mark_publication(self, cycle: str, status: str, retry_at: int = 0, error: Optional[str] = None) -> None:
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return
            state = self.load_state()
            item = state.setdefault("cycles", {}).setdefault(cycle, {"files": {}})
            item.update({"publication": status, "publish_retry_at": retry_at, "publication_error": error, "updated_at": utc_now()})
            self.save_state(state)

    @staticmethod
    def attempt_cause(code, status, error, elapsed, budget):
        text = str(error or '').lower()
        if status == 429:
            return 'rate_limited'
        if code == 28:
            if 'operation too slow' in text or 'less than' in text and 'bytes/sec' in text:
                return 'low_speed_timeout'
            if elapsed >= budget - 1:
                return 'total_timeout'
            if status == 0:
                return 'connection_failure'
            return 'timeout_unknown'
        if code in {5, 6, 7, 35, 51, 60}:
            return 'connection_failure'
        if code == 0 and status < 400:
            return ''
        if status >= 500:
            return 'upstream_temporary'
        if status >= 400:
            return 'upstream_http_error'
        return 'transfer_failure'

    def history_file(self, cycle, write=False):
        cycle = safe_cycle(cycle)
        directory = self.root / '.pool-attempts'
        if write:
            directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.exists():
            return None
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('unsafe attempt history directory')
        directory_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(cycle + '.jsonl', (os.O_RDWR | os.O_APPEND | os.O_CREAT if write else os.O_RDONLY) | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        finally:
            os.close(directory_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            os.close(fd)
            raise ValueError('unsafe attempt history file')
        return fd

    def record_attempt(self, cycle, value):
        fd = self.history_file(cycle, write=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # Isolate a torn last record after a crash; never rewrite old evidence.
            if os.fstat(fd).st_size:
                if os.pread(fd, 1, os.fstat(fd).st_size-1) != b'\n':
                    os.write(fd, b'\n')
            data = (json.dumps(value, sort_keys=True) + '\n').encode()
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError('attempt history write failed')
                data = data[written:]
            os.fsync(fd)
        except OSError as exc:
            raise RuntimeError('attempt history could not be persisted; inspect before retry') from exc
        finally:
            os.close(fd)

    def attempt_history(self, cycle, before=None, limit=30):
        """Bound memory, retain every record; cursor is the parent start offset."""
        if before is not None and (type(before) is not int or before < 0):
            raise ValueError('invalid history cursor')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid history limit')
        fd = self.history_file(cycle)
        if fd is None:
            return {'items': [], 'next_cursor': None, 'legacy_unrecorded': True}
        from collections import OrderedDict
        rows = OrderedDict()
        damaged = 0
        with os.fdopen(fd, 'rb') as stream:
            end = os.fstat(stream.fileno()).st_size
            while stream.tell() < end:
                offset = stream.tell()
                line = stream.readline(min(16384, end-offset))
                if not line.endswith(b'\n'):
                    damaged += 1
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    damaged += 1
                    continue
                key = event.get('id')
                if event.get('event') == 'start' and (before is None or offset < before):
                    rows[key] = {k:v for k,v in event.items() if k not in {'event','owner'}}
                    rows[key].update(status='unfinished', offset=offset, requests=[])
                    while len(rows) > limit+1:
                        rows.popitem(last=False)
                elif key in rows:
                    if event.get('event') == 'finish':
                        rows[key].update({k:v for k,v in event.items() if k not in {'event'}})
                    elif event.get('event') in {'request_start','request_finish'}:
                        requests = rows[key]['requests']
                        value = {k:v for k,v in event.items() if k not in {'event','id'}}
                        prior = next((r for r in requests if r['request'] == value['request']), None)
                        if prior is None:
                            requests.append(value)
                        else:
                            prior.update(value)
        items = list(rows.values())[-limit:][::-1]
        return {'items':items, 'next_cursor':items[-1]['offset'] if len(rows)>limit else None,
                'legacy_unrecorded':False, 'damaged_records':damaged}

    def invoke_curl(self, command, **kwargs):
        context = getattr(self, '_attempt_context', None)
        if context is None:
            return subprocess.run(command, **kwargs)
        context['request'] += 1
        output = Path(command[command.index('--output')+1])
        before = output.stat().st_size if output.exists() else 0
        start = time.monotonic()
        budget = float(command[command.index('--max-time')+1])
        record = {'id':context['id'], 'request':context['request'], 'event':'request_start',
                  'started_at':utc_now(), 'bytes_before':before, 'budget_seconds':budget}
        if context.get('transfer_node'):
            record['transfer_node'] = context['transfer_node']
        self.record_attempt(context['cycle'], record)
        try:
            result = subprocess.run(command, **kwargs)
        except OSError:
            elapsed = time.monotonic()-start
            self.record_attempt(context['cycle'], dict(record, event='request_finish', finished_at=utc_now(),
                execution_seconds=elapsed, cause='command_failure', status='failed', curl_code=None))
            raise
        elapsed = time.monotonic()-start
        headers = Path(str(output)+'.headers')
        status = self.response_status(headers, result)
        error = result.stderr or ''
        after = output.stat().st_size if output.exists() else 0
        self.record_attempt(context['cycle'], dict(record, event='request_finish', finished_at=utc_now(),
            execution_seconds=elapsed, bytes_after=after, http_status=status, curl_code=result.returncode,
            cause=self.attempt_cause(result.returncode,status,error,elapsed,budget),
            status='succeeded' if result.returncode==0 else 'failed'))
        return result

    def run_curl(self, command, **kwargs):
        """Retry only zero-progress NOMADS connection failures via fresh DNS peers."""
        deadline = time.monotonic() + float(command[command.index("--max-time") + 1])
        output = Path(command[command.index("--output") + 1])
        before = output.stat().st_size if output.exists() else 0
        result = self.invoke_curl(command, **kwargs)
        url = urlsplit(command[-1])
        if (self.source != "gfs" or url.scheme != "https"
                or url.hostname != "nomads.ncep.noaa.gov"
                or result.returncode not in {7, 28, 35}
                or (output.stat().st_size if output.exists() else 0) != before):
            return result
        try:
            peers = sorted({item[4][0] for item in socket.getaddrinfo(
                url.hostname, 443, socket.AF_INET, socket.SOCK_STREAM)})[:2]
        except OSError:
            return result
        for peer in peers:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                break
            retry = list(command)
            retry[retry.index("--max-time") + 1] = str(max(1, int(remaining)))
            retry[retry.index("--connect-timeout") + 1] = str(min(
                float(command[command.index("--connect-timeout") + 1]), max(1, int(remaining))))
            # Preserve HTTPS hostname/SNI/certificate checks and the original
            # Range/continuation destination; never use a mirror or literal-IP URL.
            retry[-1:-1] = ["--resolve", url.hostname + ":443:" + peer]
            result = self.invoke_curl(retry, **kwargs)
            if (result.returncode not in {7, 28, 35}
                    or (output.stat().st_size if output.exists() else 0) != before):
                break
        return result

    def probe_routes(self, probe, routes):
        error = ""
        for _, url in routes:
            command = ["curl", "--fail", "--location", "--range", "0-16383", "--max-filesize", "16384",
                       "--connect-timeout", "10", "--max-time", "30", "--silent", "--show-error",
                       "--output", str(probe), url]
            result = self.run_curl(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   universal_newlines=True, check=False, pass_fds=self.worker_fds)
            try:
                with probe.open("rb") as stream:
                    if result.returncode == 0 and stream.read(4) == b"GRIB":
                        return True, ""
            except FileNotFoundError:
                pass
            finally:
                try:
                    probe.unlink()
                except FileNotFoundError:
                    pass
            error = (result.stderr or "").strip()[-240:] or error
        return False, error

    def probe_published(self, cycle: str) -> bool:
        """Probe f072 before spending bandwidth on low-priority bulk prefetch."""
        probe = self.root / f".publish-probe-{cycle}.part"
        available, error = self.probe_routes(probe, self.transfer_routes(cycle, 72)[1])
        if available:
            self._mark_publication(cycle, "published")
            return True
        self._mark_publication(cycle, "waiting_publish", epoch_now() + self.args.publish_probe_seconds, error or "f072 尚未发布")
        return False

    def _pick_next(self) -> Optional[Tuple[str, int]]:
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return None
            state = self.load_state()
            now = epoch_now()
            choices = []
            probe_cycle = None
            for cycle, cycle_state in state.get("cycles", {}).items():
                if not CYCLE_RE.fullmatch(cycle):
                    continue
                files = cycle_state.get("files", {})
                has_task = any(entry.get("priority") == "task" and entry.get("status") not in {"ready", "failed"} for entry in files.values() if isinstance(entry, dict))
                for key, entry in files.items():
                    if not isinstance(entry, dict) or not key.isdigit() or entry.get("status") in {"obsolete","cancelled"}:
                        continue
                    hour = int(key)
                    path = self.file_path(cycle, hour)
                    if self.valid(path):
                        if entry.get("status") != "ready":
                            entry.update({"status": "ready", "bytes": path.stat().st_size, "retry_at": 0})
                        continue
                    status = entry.get("status", "queued")
                    if status == "retry_wait":
                        terminal_retry_at = int(entry.get("terminal_retry_at") or 0)
                        if terminal_retry_at > now:
                            continue
                        entry.update({
                            "status": "queued",
                            "retry_at": 0,
                            "terminal_retry_at": 0,
                            "attempts": 0,
                            "no_progress_attempts": 0,
                            "error": None,
                            "failure_class": "",
                            "failed_at": 0,
                            "retryable": False,
                            "updated_at": utc_now(),
                        })
                        status = "queued"
                    if status == "failed" or int(entry.get("retry_at") or 0) > now:
                        continue
                    priority = 0 if entry.get("priority") == "task" else 1
                    # A prefetch cycle is eligible only after f072 has appeared.
                    if priority and not has_task and cycle_state.get("publication") != "published":
                        if int(cycle_state.get("publish_retry_at") or 0) <= now:
                            probe_cycle = max(probe_cycle or cycle, cycle)
                        continue
                    choices.append((priority, -int(cycle), cycle, hour))
            self.save_state(state)
        if choices:
            choices.sort()
            priority, _cycle_order, cycle, hour = choices[0]
            # An older bulk fill must not starve a due publication check for
            # the new target. Task-priority files still take precedence, and
            # the probe's existing retry deadline bounds repeated checks.
            if priority and probe_cycle:
                return probe_cycle, -1
            return cycle, hour
        return (probe_cycle, -1) if probe_cycle else None

    @staticmethod
    def _retry_after(headers: Path, attempts: int, maximum: int) -> int:
        retry_after = ""
        try:
            for line in headers.read_text(encoding="iso-8859-1", errors="replace").splitlines():
                if line.lower().startswith("retry-after:"):
                    retry_after = line.split(":", 1)[1].strip()
        except OSError:
            pass
        if retry_after.isdigit():
            return min(maximum, max(1, int(retry_after)))
        if retry_after:
            try:
                retry_at = email.utils.parsedate_to_datetime(retry_after).astimezone(timezone.utc).timestamp()
                return min(maximum, max(1, int(retry_at - time.time())))
            except (TypeError, ValueError, IndexError):
                pass
        return min(maximum, 45 * max(1, attempts) ** 2)

    @staticmethod
    def _http_status(headers: Path) -> int:
        try:
            values = [line.strip().split()[1] for line in headers.read_text(encoding="iso-8859-1", errors="replace").splitlines() if line.strip().startswith("HTTP/") and len(line.strip().split()) >= 2]
            return int(values[-1]) if values else 0
        except (OSError, ValueError):
            return 0

    def response_status(self, headers, result):
        # Older curl may leave an empty header dump on --fail.
        stats = str(result.stdout or "").split()
        if len(stats) == 2 and re.fullmatch(r"[1-5][0-9]{2}", stats[1]):
            return int(stats[1])
        status = self._http_status(headers)
        if not status and result.returncode == 22:
            match = re.search(r"requested URL returned error:\s*([45][0-9]{2})", result.stderr or "", re.I)
            if match:
                return int(match[1])
        return status

    def _download_one(self, cycle: str, hour: int) -> None:
        # Each provider has its own inherited slot; worker/cycle gates remain shared
        # with legacy workers. A surviving curl retains its provider slot.
        with kernel_guard(self.root / '.pool-download-slot', blocking=False) as transfer:
            if transfer is None:
                self._set_wait(cycle, 'source_transfer_busy')
                time.sleep(.2)
                return
            previous = self.worker_fds
            self.worker_fds = (*previous,transfer)
            try:
                with exclusive(self.cycle_lock(cycle), blocking=False) as cycle_guard:
                    if cycle_guard is None:
                        self._set_wait(cycle, 'cycle_busy')
                        time.sleep(.2)
                        return
                    self._set_wait(cycle, '')
                    self._download_one_locked(cycle, hour)
            finally:
                self.worker_fds = previous

    def _set_wait(self, cycle: str, reason: str) -> None:
        # Store codes only; readers do not expose remote paths or raw errors.
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return
            state = self.load_state()
            item = state.get('cycles', {}).get(cycle)
            if item is not None:
                item['wait_code'] = reason
                item['wait_observed_at'] = epoch_now()
                self.save_state(state)

    def _download_one_locked(self, cycle: str, hour: int) -> None:
        output = self.file_path(cycle, hour)
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.valid(output):
            self._set_entry(cycle, hour, {"status": "ready", "bytes": output.stat().st_size, "retry_at": 0, "failure_class": "", "failed_at": 0, "retryable": False, "terminal_retry_at": 0, "error": None})
            return
        part = Path(str(output) + ".part")
        headers = Path(str(part) + ".headers")
        before = part.stat().st_size if part.exists() else 0
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return
            state = self.load_state()
            entry = state["cycles"][cycle]["files"][f"{hour:03d}"]
            attempts = int(entry.get("attempts") or 0) + 1
            attempt_id = uuid.uuid4().hex
            started_at, started_clock = utc_now(), time.monotonic()
            self.record_attempt(cycle, {'event':'start','id':attempt_id,'file':self.file_name(hour),
                'attempt':attempts,'started_at':started_at,'bytes_before':before,
                'min_speed_bps':self.args.min_speed,'low_speed_seconds':120,'budget_seconds':3600})
            entry.update({"status": "downloading", "attempts": attempts, "bytes": before, "current_file": self.file_name(hour), "retry_at": 0, "error": None, "updated_at": utc_now()})
            state["cycles"][cycle]["active_file"] = self.file_name(hour)
            state["cycles"][cycle]["updated_at"] = utc_now()
            self.save_state(state)
        command = ["curl", "--fail", "--location", "--continue-at", "-", "--connect-timeout", "30", "--max-time", "3600", "--speed-time", "120", "--speed-limit", str(self.args.min_speed), "--dump-header", str(headers), "--output", str(part), "--write-out", "%{speed_download} %{http_code}", self.url(cycle, hour)]
        command_error = ""
        self._attempt_context = {'id':attempt_id, 'cycle':cycle, 'request':0}
        try:
            result = self.transfer_file(cycle, hour, command, entry, before,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False, pass_fds=self.worker_fds)
        except OSError as exc:
            result = None
            command_error = f"download command failed: {exc}"
        finally:
            self._attempt_context = None
        speed_value = ((result.stdout or "").split() or [""])[0] if result is not None else ""
        speed = int(float(speed_value)) if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", speed_value) else 0
        if result is not None and result.returncode == 0 and self.valid(part):
            os.replace(part, output)
            try:
                headers.unlink()
            except FileNotFoundError:
                pass
            self.record_attempt(cycle, {'event':'finish','id':attempt_id,'status':'succeeded',
                'finished_at':utc_now(),'execution_seconds':time.monotonic()-started_clock,
                'bytes_after':output.stat().st_size,'cause':''})
            self._set_entry(cycle, hour, {"status": "ready", "bytes": output.stat().st_size, "sha256": self.digest(output), "download_rate_bps": speed, "retry_at": 0, "no_progress_attempts": 0, "error": None, "failure_class": "", "failed_at": 0, "retryable": False, "terminal_retry_at": 0})
            return
        after = part.stat().st_size if part.exists() else 0
        status = self.response_status(headers, result) if result is not None else 0
        if command_error:
            error = command_error[-240:]
        elif result is not None and result.returncode == 0:
            error = "downloaded file failed GRIB integrity validation"
        else:
            error = (result.stderr.strip() or f"HTTP {status or 'unknown'}")[-240:]
        # A server that ignores Range cannot safely append.  Preserve evidence
        # and restart a clean partial file on the next attempt.
        if part.exists() and before and (status == 416 or "range" in error.lower()):
            preserved = Path(f"{part}.unresumable.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
            os.replace(part, preserved)
            after = 0
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return
            state = self.load_state()
            entry = state["cycles"][cycle]["files"][f"{hour:03d}"]
            no_progress = int(entry.get("no_progress_attempts") or 0) + (1 if after <= before else 0)
            failure_class, classified_retryable = self.classify_error(status, error)
            retryable = classified_retryable
            terminal = (
                (not retryable and (status >= 400 or failure_class != "unknown"))
                or attempts >= self.args.retry_max_attempts
                or no_progress >= self.args.retry_max_attempts
            )
            values = {"bytes": after, "download_rate_bps": speed, "no_progress_attempts": no_progress, "error": error, "http_status": status, "failure_class": failure_class, "failed_at": epoch_now()}
            if terminal:
                values.update({
                    "status": "retry_wait" if retryable else "failed",
                    "retry_at": 0,
                    "retryable": retryable,
                    "terminal_retry_at": epoch_now() + self.args.retry_max_seconds if retryable else 0,
                })
            else:
                values.update({"status": "rate_limited" if status == 429 else "queued", "retry_at": epoch_now() + self._retry_after(headers, attempts, self.args.retry_max_seconds)})
            cause = self.attempt_cause(result.returncode if result is not None else None,
                status, error, time.monotonic()-started_clock, 3600)
            if command_error:
                cause = 'command_failure'
            elif result is not None and result.returncode == 0:
                cause = 'data_integrity'
            self.record_attempt(cycle, {'event':'finish','id':attempt_id,'status':'failed',
                'finished_at':utc_now(),'execution_seconds':time.monotonic()-started_clock,
                'bytes_after':after,'cause':cause,'curl_code':result.returncode if result is not None else None,
                'http_status':status,'next_retry_at':values.get('retry_at') or values.get('terminal_retry_at') or 0})
            entry.update(values)
            state["cycles"][cycle]["active_file"] = None
            state["cycles"][cycle]["updated_at"] = utc_now()
            self.save_state(state)
        try:
            headers.unlink()
        except FileNotFoundError:
            pass

    def _write_manifest_if_complete(self, cycle: str) -> None:
        expected_hours = self.expected_hours()
        paths = [self.file_path(cycle, hour) for hour in expected_hours]
        # Check the cheap GRIB head/tail invariant for the whole cycle before
        # hashing anything.  Otherwise every retry of one missing forecast
        # hour re-hashes all earlier multi-hundred-MB files and can starve a
        # newly queued task behind an incomplete prefetch cycle.
        if not all(self.valid(path) for path in paths):
            return
        files = []
        for hour, path in zip(expected_hours, paths):
            files.append({"name": path.name, "forecast_hour": hour, "size": path.stat().st_size, "sha256": self.digest(path)})
        manifest = {
            "provider": self.source,
            "product": "gfs-0p25-global" if self.source == "gfs" else ("ecmwf-ifs-0p1" if self.source=="ecmwf_0p1" else "ecmwf-ifs-0p25"),
            "scope": "global", "cycle": cycle, "forecast_hours": list(expected_hours), "complete": True,
            "files": files, "updated_at": utc_now(), "managed_by": "backend_wrf_remote_pool",
        }
        if self.source == "gfs":
            manifest.update({"source": "NOAA NOMADS", "bounds": [-180.0, -90.0, 180.0, 90.0]})
        else:
            manifest["source"] = "ECMWF IFS"
        atomic_json(self.root / cycle / "manifest.json", manifest)

    def worker(self) -> int:
        with exclusive_process(self.worker_lock) as worker:
            if worker is None:
                return 3
            self.worker_fds = (worker,)
            with exclusive(self.state_lock) as lock:
                if lock is None:
                    return 3
                state = self.load_state()
                state['transport_policy'] = 'per-provider-1'
                self.save_state(state)
            self.recover_interrupted_downloads()
            while True:
                picked = self._pick_next()
                if picked is None:
                    # A task may be rate-limited. Keep the worker responsive to
                    # a newly enqueued higher-priority task while it waits.
                    with exclusive(self.state_lock) as lock:
                        state = self.load_state() if lock is not None else {}
                    has_pending_task = any(
                        entry.get("priority") == "task" and entry.get("status") not in {"ready", "failed"}
                        for item in state.get("cycles", {}).values() if isinstance(item, dict)
                        for entry in (item.get("files") or {}).values() if isinstance(entry, dict)
                    )
                    if not has_pending_task:
                        return 0
                    time.sleep(10)
                    continue
                cycle, hour = picked
                if hour == -1:
                    self.probe_published(cycle)
                    continue
                if self.source == "gfs" and self.args.request_interval:
                    time.sleep(self.args.request_interval)
                self._download_one(cycle, hour)
                self._write_manifest_if_complete(cycle)

    def recover_interrupted_downloads(self) -> None:
        """Requeue files left in downloading state by a stopped worker."""
        with exclusive(self.state_lock) as lock:
            if lock is None:
                return
            state = self.load_state()
            changed = False
            for cycle_state in state.get("cycles", {}).values():
                if not isinstance(cycle_state, dict):
                    continue
                cycle_changed = False
                for entry in (cycle_state.get("files") or {}).values():
                    if not isinstance(entry, dict) or entry.get("status") != "downloading":
                        continue
                    entry.update({
                        "status": "queued",
                        "retry_at": 0,
                        "current_file": None,
                        "error": "worker restarted before download completion",
                        "failure_class": "worker_interrupted",
                        "updated_at": utc_now(),
                    })
                    changed = True
                    cycle_changed = True
                if cycle_changed:
                    cycle_state["active_file"] = None
                    cycle_state["updated_at"] = utc_now()
            if changed:
                self.save_state(state)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enqueue", "worker", "forget", "cleanup", "snapshot", "release", "expire-prefetch"))
    parser.add_argument("--source", choices=("gfs", "ecmwf", "ecmwf_0p1"), required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--cycle")
    parser.add_argument("--hours")
    parser.add_argument("--expected-digest")
    parser.add_argument("--reference")
    parser.add_argument("--keep-cycles", default="")
    parser.add_argument("--priority", choices=("task", "prefetch"), default="prefetch")
    parser.add_argument("--min-bytes", type=int, default=1048576)
    parser.add_argument("--min-speed", type=int, default=1024)
    parser.add_argument("--request-interval", type=int, default=0)
    parser.add_argument("--retry-max-attempts", type=int, default=8)
    parser.add_argument("--retry-max-seconds", type=int, default=900)
    parser.add_argument("--publish-probe-seconds", type=int, default=300)
    parser.add_argument("--history-retention-days", type=int, default=30)
    parser.add_argument("--ec-template", default="")
    args = parser.parse_args()
    if args.action == "enqueue" and (not args.cycle or args.hours is None):
        parser.error("enqueue requires --cycle and --hours")
    if args.action in {"forget", "cleanup", "snapshot", "release"} and not args.cycle:
        parser.error("forget/cleanup requires --cycle")
    return args


class SplitEcmwfQueue(PoolQueue):
    """A 0.1-degree forecast step commits only after both level types exist.

    Component queues reuse the established retry and history implementation.
    Their state and payloads stay inside the logical cycle, so its existing
    cycle lock and directory retirement protect every component together.
    """
    def file_name(self, hour):
        return f"ecmwf.t00z.ifs.0p1.f{hour:03d}.grib2"

    def valid(self, path):
        # A GRIB sentinel cannot distinguish a lone SFC file from the pair.
        # Bind readiness to the atomic assembly record and exact file identity.
        if path.is_symlink() or not super().valid(path):
            return False
        match=re.fullmatch(r"ecmwf\.t00z\.ifs\.0p1\.f([0-9]{3})\.grib2",path.name)
        if not match:
            return False
        commit=read_json(path.parent/'.components'/match.group(1)/'commit.json')
        info=path.stat()
        components=commit.get('components',{})
        return (commit.get('product')=='ecmwf-ifs-0p1' and commit.get('cycle')==path.parent.name
                and commit.get('name')==path.name and set(components)=={'sfc','pl'}
                and all(isinstance(v,dict) and re.fullmatch(r'[0-9a-f]{64}',str(v.get('sha256','')))
                        and type(v.get('bytes')) is int and v['bytes']>0 for v in components.values())
                and sum(v['bytes'] for v in components.values())==info.st_size
                and [commit.get(k) for k in ('device','inode','bytes','mtime_ns')]
                    ==[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns])

    def component_name(self, hour, component):
        return f"ecmwf.t00z.ifs.0p1.f{hour:03d}.{component}.grib2"

    def component_url(self, cycle, hour, component):
        if component not in ('sfc','pl'):
            raise ValueError('Unknown IFS component')
        if self.args.ec_template:
            if '{levtype}' not in self.args.ec_template or '/0p1/' not in self.args.ec_template:
                raise ValueError('0.1-degree template must explicitly bind 0p1 and levtype')
            return (self.args.ec_template.replace('{levtype}',component).replace('{cycle}',cycle)
                    .replace('{date}',cycle[:8]).replace('{hour}',cycle[8:])
                    .replace('{fh}',f'{hour:03d}').replace('{step}',str(hour)))
        return (f'{ECMWF_PORTAL}/{cycle[:8]}/{cycle[8:]}z/ifs/0p1/oper/'
                f'{cycle}0000-{hour}h-oper-fc-{component}.grib2')

    def component_routes(self, cycle, hour, component):
        original = self.component_url(cycle, hour, component)
        if self.args.ec_template:
            return "custom", [("custom", original)]
        portal = ("ecmwf_portal", original)
        mirror = ("ecmwf_cloudfront", ECMWF_CLOUDFRONT + original[len(ECMWF_PORTAL):])
        return "ecmwf_portal", [portal, mirror] if component == "sfc" else [mirror, portal]

    def url(self, cycle, hour):
        return self.component_url(cycle,hour,'sfc')

    def part_size(self, cycle, hour):
        return sum(p.stat().st_size for p in (self.root/cycle).glob(
            f'ecmwf.t00z.ifs.0p1.f{hour:03d}.*.grib2.part') if p.is_file())

    def probe_published(self, cycle):
        for component in ('sfc','pl'):
            path=self.root/f'.publish-probe-{cycle}-{component}.part'
            available, error = self.probe_routes(path, self.component_routes(cycle, 72, component)[1])
            if not available:
                self._mark_publication(cycle,'waiting_publish',epoch_now()+self.args.publish_probe_seconds,
                                       error or f'0p1 f072 {component} 尚未发布')
                return False
        self._mark_publication(cycle,'published')
        return True

    def _download_one_locked(self, cycle, hour):
        output=self.file_path(cycle,hour)
        if self.valid(output):
            self._set_entry(cycle,hour,{'status':'ready','bytes':output.stat().st_size,'retry_at':0})
            return
        if output.parent.is_symlink():
            raise ValueError('IFS cycle directory cannot be a symbolic link')
        output.parent.mkdir(parents=True,exist_ok=True)
        for name in (self.file_name(hour), self.component_name(hour,'sfc'), self.component_name(hour,'pl')):
            for suffix in ('', '.part', '.part.headers', '.assembling'):
                candidate=output.parent/(name+suffix)
                if candidate.exists() or candidate.is_symlink():
                    info=candidate.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1:
                        raise ValueError('Unsafe IFS component path')
        paths=[];components={}
        for kind in ('sfc','pl'):
            queue=_EcmwfComponentQueue(self,cycle,hour,kind)
            queue.worker_fds=self.worker_fds
            queue.enqueue(cycle,[hour],'task')
            queue._mark_publication(cycle,'published')
            self._set_entry(cycle,hour,{'status':'downloading','current_file':queue.file_name(hour),
                'active_component':kind,'retry_at':0,'error':None})
            picked=queue._pick_next()
            path=queue.file_path(cycle,hour)
            if picked and picked==(cycle,hour):queue._download_one_locked(cycle,hour)
            entry=queue.load_state()['cycles'][cycle]['files'][f'{hour:03d}']
            if entry.get('status')!='ready' or not queue.valid(path):
                # Only scheduling/diagnostic fields propagate. Logical task and
                # prefetch references are never replaced by component state.
                fields=('status','attempts','bytes','current_file','retry_at','terminal_retry_at',
                        'retryable','failure_class','failed_at','error','http_status','no_progress_attempts')
                values={k:entry[k] for k in fields if k in entry}
                if values.get('status')=='ready':values['status']='queued'
                self._set_entry(cycle,hour,values)
                return
            paths.append(path)
            components[kind]={'sha256':entry.get('sha256') or self.digest(path),'bytes':path.stat().st_size}
        temporary=Path(str(output)+'.assembling')
        descriptor=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            for path in paths:
                with path.open('rb') as source:shutil.copyfileobj(source,stream,1024*1024)
            stream.flush();os.fsync(stream.fileno())
        if not PoolQueue.valid(self,temporary):raise RuntimeError('IFS component assembly failed')
        digest=self.digest(temporary)
        os.replace(temporary,output)
        info=output.stat()
        atomic_json(output.parent/'.components'/f'{hour:03d}'/'commit.json',
            {'product':'ecmwf-ifs-0p1','cycle':cycle,'name':output.name,'device':info.st_dev,
             'inode':info.st_ino,'bytes':info.st_size,'mtime_ns':info.st_mtime_ns,
             'sha256':digest,'components':components})
        self._set_entry(cycle,hour,{'status':'ready','bytes':output.stat().st_size,
            'sha256':digest,'components':components,'active_component':None,
            'retry_at':0,'terminal_retry_at':0,'failure_class':'','error':None})
        # Raw complete components are redundant after the atomic logical commit.
        # Interrupted components and every historical attempt remain retained.
        for path in paths:path.unlink()


class _EcmwfComponentQueue(PoolQueue):
    def __init__(self,parent,cycle,hour,component):
        for path in (parent.root/cycle, parent.root/cycle/'.components', parent.root/cycle/'.components'/f'{hour:03d}', parent.root/cycle/'.components'/f'{hour:03d}'/component):
            if path.is_symlink():
                raise ValueError('IFS component state cannot be a symbolic link')
        values=dict(vars(parent.args));values.update(source='ecmwf',
            root=str(parent.root/cycle/'.components'/f'{hour:03d}'/component),
            ec_template=parent.component_url(cycle,hour,component))
        super().__init__(argparse.Namespace(**values))
        self.parent,self.logical_cycle,self.component=parent,cycle,component

    def file_name(self,hour):return self.parent.component_name(hour,self.component)

    def file_path(self,cycle,hour):return self.parent.root/cycle/self.file_name(hour)

    def transfer_routes(self, cycle, hour):
        return self.parent.component_routes(cycle, hour, self.component)

    def record_attempt(self,cycle,event):
        return self.parent.record_attempt(self.logical_cycle,dict(event,component=self.component))


def main() -> int:
    args = arguments()
    queue = (SplitEcmwfQueue if args.source=='ecmwf_0p1' else PoolQueue)(args)
    if args.action == "enqueue":
        queue.enqueue(args.cycle, parse_hours(args.hours), args.priority,args.reference)
        print(f"QUEUED|{args.cycle}|{args.priority}")
        return 0
    if args.action == "forget":
        queue.forget(args.cycle)
        print(f"FORGOTTEN|{args.cycle}")
        return 0
    if args.action == "cleanup":
        print(f"{queue.cleanup(args.cycle,args.expected_digest)}|{queue.root / args.cycle}")
        return 0
    if args.action == "snapshot":
        print(json.dumps(queue.snapshot(args.cycle)))
        return 0
    if args.action == "release":
        queue.release_reference(args.cycle,args.reference)
        print(json.dumps({"reference":args.reference,"status":"released"}))
        return 0
    if args.action == "expire-prefetch":
        queue.expire_prefetch([cycle for cycle in args.keep_cycles.split(',') if cycle])
        print(json.dumps({"status":"expired"}))
        return 0
    return queue.worker()


if __name__ == "__main__":
    raise SystemExit(main())
