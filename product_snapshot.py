"""Stable identity and bounded reads for ordinary WRF display products."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from runtime_assets import checked_path, output_file


MAX_FRAME_BYTES = 32 * 1024 * 1024
TERMINAL_PRODUCTS = {"succeeded", "partial_success"}


class ProductSnapshotError(ValueError):
    pass


def product_identity(task: dict) -> dict:
    """Use persisted build metadata, never the time of a read request."""
    attempt = int(task.get("attempt_no") or 1)
    result = task.get("result") or {}
    meta = result.get("meta_json") or {}
    version = None
    if (task.get("status") in TERMINAL_PRODUCTS and meta
            and int(result.get("attempt_no") or attempt) == attempt):
        payload = ["wrf-product-1", task.get("id"), attempt, meta]
        version = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False).encode()).hexdigest()
    return {"attempt_no": attempt, "product_version": version}


def bind_display(task: dict, result: dict) -> dict:
    identity = product_identity(task)
    if not identity["product_version"]:
        raise ProductSnapshotError("结果已更新或本次尝试尚无可展示产品，请刷新任务状态。")
    return {**result, **identity}


def read_frame(task: dict, display: dict, output_dir: Path, path: str,
               attempt_no: int, product_version: str, lookup, *, settings=None) -> bytes:
    """Read a manifest-listed frame and recheck identity after reading bytes."""
    expected = {"attempt_no": attempt_no, "product_version": product_version}
    if not product_version or product_identity(task) != expected:
        raise ProductSnapshotError("产品版本已变化，请刷新结果后重试。")
    listed = {str(frame.get("url") or ""): frame
              for domain in (display.get("meta_json") or {}).get("domains", [])
              for variable in domain.get("variables", [])
              for frame in variable.get("frames", [])}
    parsed = urlsplit(path)
    prefix = f"/data/WRF/runs/{task['id']}/"
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
            or path not in listed or not path.startswith(prefix) or not path.endswith(".webp")):
        raise FileNotFoundError("产品不存在或不可访问")
    relative = path.removeprefix("/data/WRF/")
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise FileNotFoundError("产品路径无效")
    target = (output_file(settings, task, path.removeprefix(prefix)) if settings is not None
              else checked_path(Path(output_dir) / "runs" / task["id"], path.removeprefix(prefix)))
    with target.open("rb") as stream:
        content = stream.read(MAX_FRAME_BYTES + 1)
    if len(content) > MAX_FRAME_BYTES:
        raise ProductSnapshotError("该展示帧过大，请在工作台查看。")
    checksum = listed[path].get("sha256")
    if checksum and hashlib.sha256(content).hexdigest() != checksum:
        raise ProductSnapshotError("产品正在更新或校验失败，请刷新结果后重试。")
    # Returning bytes prevents FileResponse from opening a newer file later.
    if product_identity(lookup(task["id"])) != expected:
        raise ProductSnapshotError("任务或产品版本已变化，请刷新结果后重试。")
    return content
