"""Exact, read-only legacy asset mappings for an explicitly migrated database."""
import hashlib
import json
from pathlib import Path
import re

from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException


SCHEMA = "wrf-local-assets-1"
COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,160}")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def task_identity(task):
    return digest([task.get("id"), task.get("owner_sub") or "", task.get("created_at")])


def checked_path(root, relative):
    """Allow a configured root, but no descendant symlink or traversal."""
    root = Path(root).resolve()
    relative = str(relative)
    parts = relative.split("/")
    if Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise FileNotFoundError("Asset path is invalid")
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise FileNotFoundError("Asset symlinks are not permitted")
    if not candidate.resolve().is_relative_to(root):
        raise FileNotFoundError("Asset escaped its task root")
    return candidate


def legacy_entry(settings, task, attempt_no=None):
    path = getattr(settings, "legacy_asset_manifest", None)
    if path is None or not Path(path).is_file():
        return None
    if Path(path).is_symlink() or Path(path).stat().st_size > 16 * 1024 * 1024:
        raise FileNotFoundError("Legacy asset manifest is invalid")
    try:
        manifest = json.loads(Path(path).read_text())
        if manifest.get("schema") != SCHEMA:
            raise ValueError("Unknown asset mapping")
        if Path(manifest["target_database"]).resolve() != Path(settings.database_path).resolve():
            raise ValueError("Asset mapping belongs to a different database")
        entry = manifest.get("tasks", {}).get(task["id"])
        if not entry or entry.get("identity_digest") != task_identity(task):
            return None
        attempt = int(attempt_no if attempt_no is not None else task.get("attempt_no") or 1)
        if attempt != int(entry["attempt_no"]) and str(attempt) not in entry.get("archived_logs", {}):
            return None
        return {**entry, "roots": manifest["legacy_roots"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise FileNotFoundError("Legacy asset mapping is invalid") from exc


def _task_relative(task, relative):
    task_id = str(task.get("id") or "")
    if not COMPONENT.fullmatch(task_id):
        raise FileNotFoundError("Invalid task identity")
    return task_id + "/" + str(relative)


def read_run_path(settings, task, relative, attempt_no=None):
    candidate = checked_path(settings.run_dir, _task_relative(task, relative))
    if candidate.exists():
        return candidate
    entry = legacy_entry(settings, task, attempt_no)
    if entry and int(attempt_no if attempt_no is not None else task.get("attempt_no") or 1) == int(entry["attempt_no"]):
        legacy = checked_path(entry["roots"]["run_dir"], _task_relative(task, relative))
        if legacy.exists():
            return legacy
    return candidate


def output_file(settings, task, relative):
    candidate = checked_path(settings.output_dir, "runs/" + _task_relative(task, relative))
    if candidate.is_file():
        return candidate
    entry = legacy_entry(settings, task)
    if entry and int(task.get("attempt_no") or 1) == int(entry["attempt_no"]):
        legacy = checked_path(entry["roots"]["output_dir"], "runs/" + _task_relative(task, relative))
        if legacy.is_file():
            return legacy
    raise FileNotFoundError("Product does not exist")


def log_paths(settings, task):
    paths = []
    entry = legacy_entry(settings, task)
    if entry and int(task.get("attempt_no") or 1) == int(entry["attempt_no"]):
        legacy = checked_path(entry["roots"]["run_dir"], _task_relative(task, "service.log"))
        if legacy.is_file():
            paths.append(legacy)
    current = checked_path(settings.run_dir, _task_relative(task, "service.log"))
    if current.is_file() and current not in paths:
        paths.append(current)
    return paths


def archived_log_path(settings, task, attempt_no, declared):
    relative = _task_relative(task, "attempts")
    root = checked_path(settings.run_dir, relative)
    declared = Path(declared)
    for spelling in (Path(settings.run_dir).absolute() / relative, root):
        if declared.is_absolute() and declared.is_relative_to(spelling):
            return checked_path(root, declared.relative_to(spelling).as_posix())
    entry = legacy_entry(settings, task, attempt_no)
    if entry and entry.get("archived_logs", {}).get(str(attempt_no)) == str(declared):
        legacy_root = checked_path(entry["roots"]["run_dir"], relative)
        if declared.is_absolute() and declared.is_relative_to(legacy_root):
            return checked_path(legacy_root, declared.relative_to(legacy_root).as_posix())
    raise ValueError("历史日志不在任务归档目录内")


def read_log_bytes(paths, after=0, limit=65536, tail=False):
    sizes = [(path, path.stat().st_size) for path in paths]
    size = sum(count for _, count in sizes)
    start = max(0, size - limit) if tail else min(max(0, int(after)), size)
    remaining, offset, content = max(1, min(int(limit), 262144)), start, bytearray()
    skip = start
    for path, count in sizes:
        if skip >= count:
            skip -= count
            continue
        with path.open("rb") as stream:
            stream.seek(skip)
            data = stream.read(remaining)
        content.extend(data)
        remaining -= len(data)
        offset += len(data)
        skip = 0
        if not remaining:
            break
    return offset, bytes(content)


class RuntimeStaticFiles(StaticFiles):
    """Keep the authenticated URL while allowing only mapped legacy tasks."""
    def __init__(self, settings, lookup):
        super().__init__(directory=settings.output_dir)
        self.settings, self.lookup = settings, lookup

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "runs" or not COMPONENT.fullmatch(parts[1]):
            raise HTTPException(404)
        try:
            task = self.lookup(parts[1])
            if not task:
                raise FileNotFoundError()
            candidate = output_file(self.settings, task, "/".join(parts[2:]))
            return self.file_response(str(candidate), candidate.stat(), scope)
        except (OSError, ValueError, KeyError):
            raise HTTPException(404) from None
