"""Serialize reference binding and explicit cleanup on the API host."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time


@contextmanager
def forcing_reference_guard(database_path: Path, timeout: float = 30):
    # Never unlink this inode: independently opened descriptors must continue
    # to refer to the same lock across threads, processes and API restarts.
    path = database_path.parent / ".forcing-references.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("资料引用锁归属异常，拒绝操作")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("资料引用正在更新或清理，请稍后重试")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)
