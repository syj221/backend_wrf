"""Access policy for directly served WRF run files."""

import re
from collections.abc import Callable
from typing import Any


TASK_ID = re.compile(r"^(?:wrf_(?:gfs|ecmwf)|ds)_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")


def allow_run_asset(path: str, user: dict[str, Any], lookup: Callable[[str], dict | None]) -> bool:
    if not path.startswith("/data/WRF/runs/"):
        return False
    parts = path.removeprefix("/data/WRF/runs/").split("/")
    if len(parts) < 2 or not TASK_ID.fullmatch(parts[0]) or any(part in {"", ".", ".."} for part in parts):
        return False
    task = lookup(parts[0])
    if task is None:
        return False
    return int(user.get("role") or 0) >= 3 or bool(user.get("sub")) and task.get("owner_sub") == user["sub"]
