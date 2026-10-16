"""Operator-only, audit-bound reclamation of deleted task leases."""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import time
from directory_delete import mount_for_fd, private_fuseblk_group


def read_lease(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 1048576:
            raise ValueError('Untrusted compute lease file')
        raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('Oversized compute lease')
        return json.loads(raw), hashlib.sha256(raw.encode()).hexdigest()


def no_live_processes(proof, control):
    process = proof.get('process') or {}
    pid = process.get('pid') or proof.get('legacy_pid')
    if not pid and not (proof.get('cancellation_verified') is True or proof.get('never_started') is True):
        raise ValueError('Missing retained terminal identity')
    if pid and (type(pid) is not int or pid <= 1):
        raise ValueError('Invalid retained process identity')
    observer = os.getsid(0)
    needles = [str(control.parent).encode(), control.parent.name.encode()]
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                continue
            if pid and (int(path.name) == pid or int(fields[3]) == pid):
                raise ValueError('Retained PID or session is still allocated')
            if path.stat().st_uid != os.getuid() or int(fields[3]) == observer:
                continue
            if any(needle in (path / 'cmdline').read_bytes() for needle in needles):
                raise ValueError('Task still has an unaccounted process')
        except FileNotFoundError:
            continue
        # Permission errors remain fatal: unknown processes never mean idle.


def reclaim(resource, approved, apply=False):
    resource = Path(resource)
    control = Path(approved['control'])
    if (not re.fullmatch(r'wrf_(?:gfs|ecmwf|ec)_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{8}', approved['task_id'])
            or not re.fullmatch(r'[a-f0-9]{32}', approved['attempt'])
            or not re.fullmatch(r'delete_[a-f0-9]{32}', approved['deletion_id'])):
        raise ValueError('Invalid approved lease identity')
    if (not resource.is_absolute() or not control.is_absolute()
            or control.name != 'control' or control.parent.name != approved['task_id']):
        raise ValueError('Lease control path is outside the fixed task root')
    canonical = resource.resolve(strict=True)
    if control.parent.resolve() != canonical / 'tasks' / approved['task_id']:
        raise ValueError('Task path alias changes the approved ownership')
    proof = approved['terminal']
    if (proof.get('verified') is not True or proof.get('status') not in ('cancelled', 'failed', 'succeeded')
            or proof.get('attempt') != approved['attempt']):
        raise ValueError('Deleted task terminal proof is incomplete')
    fd = os.open(str(resource / 'compute.lock'), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Untrusted compute lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = resource / 'compute-lease.json'
        expected = {'root': str(control), 'attempt': approved['attempt']}
        lease, digest = read_lease(path)
        if lease != expected:
            raise ValueError('Compute lease no longer matches the deleted attempt')
        if os.path.lexists(str(control.parent)):
            raise ValueError('Deleted task root is present or has been replaced')
        no_live_processes(proof, control)
        receipt = {'schema': 'wrf-compute-lease-reclaim-1', 'lease': lease,
                   'lease_sha256': digest, 'approval': approved, 'verified_at': time.time(), 'status': 'verified_before_archive'}
        if apply:
            audit = resource / '.compute-lease-audit'
            audit.mkdir(mode=0o700, exist_ok=True)
            info = audit.lstat()
            audit_fd = os.open(str(audit), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent_fd = os.open(str(resource), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                safe_group = not info.st_mode & 0o020 or private_fuseblk_group(audit_fd, parent_fd)
            finally:
                os.close(audit_fd); os.close(parent_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o002 or not safe_group:
                raise ValueError('Untrusted lease audit directory')
            target = audit / (approved['deletion_id'] + '.lease.json')
            record = audit / (approved['deletion_id'] + '.receipt.json')
            if os.path.lexists(str(target)) or os.path.lexists(str(record)):
                raise ValueError('Prior reclamation audit exists; inspect without replay')
            # Persist evidence before atomically archiving the exact lease under its shared lock.
            out = os.open(str(record), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out, 'w') as stream:
                json.dump(receipt, stream); stream.flush(); os.fsync(stream.fileno())
            if read_lease(path) != (lease, digest):
                raise ValueError('Lease changed before archival')
            os.rename(str(path), str(target))
            for directory in (audit, resource):
                directory_fd = os.open(str(directory), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            receipt['status'] = 'archived'
        return receipt
