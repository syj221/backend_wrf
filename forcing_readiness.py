"""Keep cached inventory separate from file integrity evidence."""


def _hours(value):
    if (
        not isinstance(value, list)
        or any(type(hour) is not int or not 0 <= hour <= 72 for hour in value)
        or len(set(value)) != len(value)
    ):
        return None
    return value


def observe_cycle(pool, cycle, required):
    """Read this snapshot; execution must still inspect the actual files."""
    result = {
        "status": "unknown",
        "missing_hours": None,
        "availability_status": "unknown",
        "integrity_status": "unverified",
    }
    if pool is None or pool.get("status") in {
        "unavailable",
        "unknown",
        "error",
    }:
        return result
    if not cycle:
        return {
            **result,
            "status": "needs_download",
            "availability_status": "missing",
        }
    available = _hours(cycle.get("available_hours"))
    missing = (
        [hour for hour in required if hour not in available]
        if isinstance(available, list)
        else None
    )
    result["missing_hours"] = missing
    # Integrity failure is stronger evidence than a stale directory count.
    invalid = cycle.get("integrity_status") in {"invalid", "failed"} or (
        cycle.get("failure_class") == "data_integrity"
    )
    if invalid:
        return {
            **result,
            "status": "needs_download",
            "availability_status": "invalid",
            "integrity_status": "invalid",
        }
    if "available_hours" in cycle and available is None:
        return result
    present = missing == [] or (
        missing is None and cycle.get("complete") is True
    )
    if not present:
        return {
            **result,
            "status": "waiting_publish"
            if cycle.get("status") == "waiting_publish"
            else "needs_download",
            "availability_status": "missing",
        }
    valid = _hours(cycle.get("valid_hours"))
    verified = (
        available is not None
        and valid is not None
        and all(hour in valid for hour in required)
        and set(valid) <= set(available)
    )
    return {
        **result,
        "status": "ready" if verified else "unknown",
        "availability_status": "present",
        "integrity_status": "verified" if verified else "unverified",
    }


def coverage_label(window):
    if window.get("availability_status") == "invalid":
        return "已有完整性失败记录，不能作为就绪资料"
    if window.get("status") == "ready":
        return "所需时次已核验；执行前仍须复核"
    if window.get("availability_status") == "present":
        return "所需文件已下载，完整性待执行前核验"
    return {"needs_download": "所需时次待下载", "waiting_publish": "等待上游发布"}.get(
        window.get("status"), "资料状态尚未核验"
    )
