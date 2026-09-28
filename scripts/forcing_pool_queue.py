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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CYCLE_RE = re.compile(r"^\d{8}00$")
HOURS = tuple(range(73))
ECMWF_HOURS = tuple(range(0, 73, 3))
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
TRANSIENT_CURL_ERRORS = {6, 7, 18, 28, 35, 52, 56}


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
def exclusive(path: Path, blocking: bool = True):
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
    """Acquire a cross-process lock using atomic mkdir.

    The provider data roots may live on an NTFS volume mounted through FUSE,
    where advisory flock behavior is not reliable across independent worker
    processes. A lock directory gives cron and API-triggered workers one
    portable single-instance gate, while the pid file lets a later worker
    recover an orphaned lock after a crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        try:
            path.mkdir()
            acquired = True
        except FileExistsError:
            pid_path = path / "pid"
            try:
                pid = int(pid_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pid = 0
            if pid > 0:
                try:
                    os.kill(pid, 0)
                except OSError:
                    pid = 0
            if pid > 0:
                yield None
                return
            shutil.rmtree(path, ignore_errors=True)
            try:
                path.mkdir()
                acquired = True
            except FileExistsError:
                yield None
                return
        if acquired:
            (path / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
        yield path if acquired else None
    finally:
        if acquired:
            shutil.rmtree(path, ignore_errors=True)


class PoolQueue:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.source = args.source
        self.root = Path(args.root).resolve()
        self.state_path = self.root / ".pool-queue.json"
        self.state_lock = self.root / ".pool-queue.lock"
        self.worker_lock = self.root / ".pool-worker.lockdir"

    def expected_hours(self) -> Tuple[int, ...]:
        """Return the forecast fields actually published by this provider."""
        return ECMWF_HOURS if self.source == "ecmwf" else HOURS

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
        state = read_json(self.state_path)
        if state.get("version") != 1 or state.get("source") != self.source or not isinstance(state.get("cycles"), dict):
            return self.default_state()
        return state

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
                f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
                f"gfs.{date}/{init}/atmos/gfs.t{init}z.pgrb2.0p25.f{hour:03d}"
            )
        template = self.args.ec_template
        if template:
            return (template.replace("{cycle}", cycle).replace("{fh}", f"{hour:03d}")
                    .replace("{date}", date).replace("{hour}", init).replace("{step}", str(hour)))
        return f"https://data.ecmwf.int/forecasts/{date}/{init}z/ifs/0p25/oper/{date}{init}0000-{hour}h-oper-fc.grib2"

    def enqueue(self, cycle: str, hours: List[int], priority: str) -> None:
        cycle = safe_cycle(cycle)
        if self.source == "ecmwf":
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
            if self.source == "ecmwf":
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

    def probe_published(self, cycle: str) -> bool:
        """Probe f072 before spending bandwidth on low-priority bulk prefetch."""
        probe = self.root / f".publish-probe-{cycle}.part"
        command = ["curl", "--fail", "--location", "--range", "0-16383", "--connect-timeout", "20", "--max-time", "45", "--silent", "--show-error", "--output", str(probe), self.url(cycle, 72)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, check=False)
        try:
            available = result.returncode == 0 and probe.read_bytes().startswith(b"GRIB")
        except OSError:
            available = False
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        if available:
            self._mark_publication(cycle, "published")
            return True
        self._mark_publication(cycle, "waiting_publish", epoch_now() + self.args.publish_probe_seconds, result.stderr.strip()[-240:] or "f072 尚未发布")
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
                    if not isinstance(entry, dict) or not key.isdigit() or entry.get("status") == "obsolete":
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
            _priority, _cycle_order, cycle, hour = choices[0]
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

    def _download_one(self, cycle: str, hour: int) -> None:
        # The provider worker lock serializes selection, while the cycle lock
        # lets cleanup remove an unrelated old cycle during a long download.
        with exclusive(self.cycle_lock(cycle), blocking=False) as cycle_guard:
            if cycle_guard is None:
                return
            self._download_one_locked(cycle, hour)

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
            entry.update({"status": "downloading", "attempts": attempts, "bytes": before, "current_file": self.file_name(hour), "retry_at": 0, "error": None, "updated_at": utc_now()})
            state["cycles"][cycle]["active_file"] = self.file_name(hour)
            state["cycles"][cycle]["updated_at"] = utc_now()
            self.save_state(state)
        command = ["curl", "--fail", "--location", "--continue-at", "-", "--connect-timeout", "30", "--speed-time", "120", "--speed-limit", str(self.args.min_speed), "--dump-header", str(headers), "--output", str(part), "--write-out", "%{speed_download}", self.url(cycle, hour)]
        command_error = ""
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False)
        except OSError as exc:
            result = None
            command_error = f"download command failed: {exc}"
        speed = int(float(result.stdout.strip() or 0)) if result is not None and result.stdout.strip().replace(".", "", 1).isdigit() else 0
        if result is not None and result.returncode == 0 and self.valid(part):
            os.replace(part, output)
            try:
                headers.unlink()
            except FileNotFoundError:
                pass
            self._set_entry(cycle, hour, {"status": "ready", "bytes": output.stat().st_size, "sha256": self.digest(output), "download_rate_bps": speed, "retry_at": 0, "no_progress_attempts": 0, "error": None, "failure_class": "", "failed_at": 0, "retryable": False, "terminal_retry_at": 0})
            return
        after = part.stat().st_size if part.exists() else 0
        status = self._http_status(headers) if result is not None else 0
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
            "product": "gfs-0p25-global" if self.source == "gfs" else "ecmwf-ifs-0p25",
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
                return 0
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
    parser.add_argument("action", choices=("enqueue", "worker", "forget"))
    parser.add_argument("--source", choices=("gfs", "ecmwf"), required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--cycle")
    parser.add_argument("--hours")
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
    if args.action == "forget" and not args.cycle:
        parser.error("forget requires --cycle")
    return args


def main() -> int:
    args = arguments()
    queue = PoolQueue(args)
    if args.action == "enqueue":
        queue.enqueue(args.cycle, parse_hours(args.hours), args.priority)
        print(f"QUEUED|{args.cycle}|{args.priority}")
        return 0
    if args.action == "forget":
        queue.forget(args.cycle)
        print(f"FORGOTTEN|{args.cycle}")
        return 0
    return queue.worker()


if __name__ == "__main__":
    raise SystemExit(main())
