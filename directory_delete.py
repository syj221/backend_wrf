"""Fixed-root quarantine deletion; no file-content inventory or hashing."""
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat


def identity(path):
    path = Path(path)
    if not path.is_absolute() or path.name in ('', '.', '..'):
        raise ValueError('Invalid managed path')
    parent = path.parent.resolve(strict=False)
    path = parent / path.name
    anchor = parent
    while not anchor.exists():
        anchor = anchor.parent
    parent_stat = anchor.stat()
    value = dict(path=str(path), parent_device=parent_stat.st_dev, parent_inode=parent_stat.st_ino,
                 exists=False, file_count=0, bytes=0, symlink_count=0)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return value
    if info.st_uid != os.getuid():
        raise ValueError('Managed root ownership changed')
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError('Managed root is a link or special file')
    return dict(value, exists=True, device=info.st_dev, inode=info.st_ino,
                kind='directory' if stat.S_ISDIR(info.st_mode) else 'file')


def intent(snapshot, operation):
    if not re.fullmatch(r'[a-zA-Z0-9_]+', operation):
        raise ValueError('Invalid operation identifier')
    path = Path(snapshot['path'])
    return dict(snapshot=snapshot, trash=str(path.parent / '.wrf-trash' / operation), name=path.name)


def parent_fd(snapshot):
    path = Path(snapshot['path'])
    fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != (snapshot['parent_device'], snapshot['parent_inode']):
        os.close(fd)
        raise ValueError('Managed parent changed')
    return fd


def mount_for_fd(fd):
    """Bind filesystem semantics to the opened directory's mount, not a path alias."""
    info = Path('/proc/self/fdinfo/' + str(fd)).read_text()
    mount_id = next((line.split(':',1)[1].strip() for line in info.splitlines() if line.startswith('mnt_id:')),None)
    device = os.fstat(fd).st_dev
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        if fields[0] == mount_id and fields[2] == '{}:{}'.format(os.major(device),os.minor(device)):
            suffix = line.split(' - ',1)[1].split()
            return suffix[0],set(suffix[2].split(','))
    raise ValueError('Quarantine mount identity is unverified')


def private_fuseblk_group(fd, parent):
    """Group-write is safe only with kernel checks and a verifiably single-user group."""
    import grp
    import pwd
    info = os.fstat(fd)
    base = os.fstat(parent)
    try:
        filesystem,options = mount_for_fd(fd)
        if filesystem != 'fuseblk' or 'default_permissions' not in options:
            return False
        import errno
        for descriptor in (fd,parent):
            try:
                acl = os.getxattr(descriptor,'system.posix_acl_access')
            except OSError as exc:
                if exc.errno not in {errno.ENODATA,errno.ENOTSUP,errno.EOPNOTSUPP}:
                    return False
            else:
                # ntfs-3g can report an empty ACL instead of ENODATA.
                # A nonempty ACL needs a separate access review.
                if acl:
                    return False
        if (info.st_gid != os.getgid() or base.st_gid != info.st_gid
                or base.st_uid != os.getuid() or base.st_mode & 0o002
                or base.st_dev != info.st_dev):
            return False
        group = grp.getgrgid(info.st_gid)
        members = {entry.pw_uid for entry in pwd.getpwall() if entry.pw_gid == info.st_gid}
        members.update(pwd.getpwnam(name).pw_uid for name in group.gr_mem)
        return members == {os.getuid()}
    except (OSError,KeyError,ValueError,IndexError,TypeError,AttributeError):
        return False


def trash_fd(parent, operation, create=False):
    current = os.dup(parent)
    try:
        for name in ('.wrf-trash', operation):
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            new = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            info = os.fstat(new)
            if (info.st_uid != os.getuid() or info.st_mode & 0o002
                    or (info.st_mode & 0o020 and not private_fuseblk_group(new,current))):
                os.close(new)
                raise ValueError('Unsafe quarantine ownership or permissions')
            os.close(current)
            current = new
        return current
    except BaseException:
        os.close(current)
        raise


def reject_mounts(path):
    # Linux exposes mounted subtrees without traversing any task file.
    table = Path('/proc/self/mountinfo')
    if table.exists():
        for line in table.read_text().splitlines():
            fields = line.split()
            if len(fields) < 5:
                raise ValueError('Mount ownership cannot be verified')
            mount = re.sub(r'\\([0-7]{3})',lambda match:chr(int(match.group(1),8)),fields[4])
            if mount == str(path) or mount.startswith(str(path).rstrip('/')+'/'):
                raise ValueError('Mounted subtree is not a task-owned directory')


def location(item):
    """Read-only classification; never infer success or replay a missing write."""
    snapshot = item['snapshot']
    if item != intent(snapshot,Path(item['trash']).name):
        raise ValueError('Invalid quarantine intent')
    source = identity(snapshot['path'])
    if source['exists'] and source != snapshot:
        raise ValueError('Managed directory identity changed')
    destination = None
    if Path(snapshot['path']).parent.exists():
        parent = parent_fd(snapshot)
        trash = None
        try:
            try:
                trash = trash_fd(parent,Path(item['trash']).name)
                destination = os.stat(item['name'],dir_fd=trash,follow_symlinks=False)
            except FileNotFoundError:
                pass
        finally:
            if trash is not None:
                os.close(trash)
            os.close(parent)
    if destination is not None:
        if (not snapshot['exists'] or (destination.st_dev,destination.st_ino) != (snapshot['device'],snapshot['inode'])
                or source['exists']):
            raise ValueError('Quarantine location conflicts with source identity')
        return 'quarantined'
    if source == snapshot:
        return 'unmoved' if source['exists'] else 'absent'
    return 'missing'


def quarantine(item):
    snapshot = item['snapshot']
    if item != intent(snapshot,Path(item['trash']).name):
        raise ValueError('Invalid quarantine intent')
    if identity(snapshot['path']) != snapshot:
        raise ValueError('Managed directory identity changed')
    if not snapshot['exists']:
        return item
    reject_mounts(snapshot['path'])
    parent = parent_fd(snapshot)
    trash = None
    try:
        trash = trash_fd(parent, Path(item['trash']).name, create=True)
        try:
            os.stat(item['name'], dir_fd=trash, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('Quarantine destination already exists; do not replay')
        info = os.stat(item['name'], dir_fd=parent, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (snapshot['device'], snapshot['inode']):
            raise ValueError('Managed directory identity changed')
        os.rename(item['name'], item['name'], src_dir_fd=parent, dst_dir_fd=trash)
        os.fsync(parent)
        os.fsync(trash)
        return item
    finally:
        if trash is not None:
            os.close(trash)
        os.close(parent)


def purge(item):
    snapshot = item['snapshot']
    if item != intent(snapshot,Path(item['trash']).name):
        raise ValueError('Invalid quarantine intent')
    if not snapshot['exists']:
        return
    reject_mounts(str(Path(item['trash'])/item['name']))
    parent = parent_fd(snapshot)
    trash = None
    try:
        trash = trash_fd(parent, Path(item['trash']).name)
        info = os.stat(item['name'], dir_fd=trash, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (snapshot['device'], snapshot['inode']):
            raise ValueError('Quarantine identity changed')
        if snapshot['kind'] == 'directory':
            if not shutil.rmtree.avoids_symlink_attacks:
                raise RuntimeError('Safe directory cleanup is unavailable')
            import inspect
            if 'dir_fd' in inspect.signature(shutil.rmtree).parameters:
                shutil.rmtree(item['name'], dir_fd=trash)
            elif Path('/proc/self/fd').is_dir():
                # Python 3.6 on the production host: pin the parent through procfs.
                shutil.rmtree('/proc/self/fd/{}/{}'.format(trash,item['name']))
            else:
                raise RuntimeError('Pinned-parent cleanup is unavailable')
        else:
            os.unlink(item['name'], dir_fd=trash)
        os.fsync(trash)
        base = os.open('.wrf-trash',os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,dir_fd=parent)
        try:
            try:
                os.rmdir(Path(item['trash']).name,dir_fd=base)
            except OSError as exc:
                import errno
                if exc.errno != errno.ENOTEMPTY:
                    raise
        finally:
            os.close(base)
    finally:
        if trash is not None:
            os.close(trash)
        os.close(parent)


def remote(client, operation, payload, timeout=30):
    program = 'import json,os,re,shutil,stat,sys\nfrom pathlib import Path\n'
    program += '\n'.join(inspect.getsource(fn) for fn in (identity,intent,parent_fd,mount_for_fd,private_fuseblk_group,trash_fd,reject_mounts,location,quarantine,purge))
    program += '\np=json.loads(sys.argv[2])\nop=sys.argv[1]\n'
    program += "if op=='identity': result=[identity(x) for x in p]\n"
    program += "elif op=='location': result=[location(x) for x in p]\n"
    program += "elif op=='quarantine': result=[quarantine(x) for x in p]\n"
    program += "elif op=='purge': result=[purge(x) for x in p]\n"
    program += "else: raise ValueError('Unknown directory operation')\nprint(json.dumps(result))\n"
    command = 'python3 -B -c ' + shlex.quote(program) + ' ' + shlex.quote(operation) + ' ' + shlex.quote(json.dumps(payload))
    kwargs = dict(timeout=timeout)
    if hasattr(client, 'allow_retries'):
        kwargs.update(_readonly=operation=='identity', _stage_budget=timeout)
    return json.loads(client.run(command, **kwargs))


def forcing_quarantine(client, source, item):
    """Reuse installed cycle/state locks; conservatively bridge legacy worker ownership."""
    settings = client._forcing_settings(source)
    module_path = str(Path(settings['script']).parent / 'forcing_pool_queue.py')
    program = 'import json,os,re,shutil,stat,sys,importlib.util\nfrom pathlib import Path\nfrom types import SimpleNamespace\n'
    program += '\n'.join(inspect.getsource(fn) for fn in (identity,intent,parent_fd,mount_for_fd,private_fuseblk_group,trash_fd,reject_mounts,location,quarantine,purge))
    program += '''
p=json.loads(sys.argv[1])
spec=importlib.util.spec_from_file_location('wrf_pool_cleanup',p['module'])
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
queue=m.PoolQueue(SimpleNamespace(source=p['source'],root=p['root']))
item=p['item']
cycle=m.safe_cycle(item['name'])
if str(queue.root/cycle)!=item['snapshot']['path']:
    raise ValueError('Cycle is outside configured forcing root')
result={'blocked':True}
# A free inherited worker gate excludes both workers and surviving curl children.
# A busy legacy gate is safe only while its original worker is demonstrably live:
# that worker holds the cycle gate for the entire curl lifetime.
def live_worker():
    marker=m.read_json(queue.worker_lock/'owner.json')
    if marker.get('protocol')!='wrf-pool-worker-2' or marker.get('root')!=str(queue.worker_lock.resolve()):return False
    pid=marker.get('pid')
    if type(pid) is not int or pid<=1:return False
    try:
        proc=Path('/proc')/str(pid)
        fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
        command=(proc/'cmdline').read_bytes().split(b'\\0')
        gate=m.LOCK_ROOT/m.hashlib.sha256(str(queue.worker_lock.resolve()).encode()).hexdigest()
        expected=gate.stat();owns_gate=False;owns_other_cycle=False
        other_gates=set()
        for path in queue.root.glob('.pool-cycle-*.lock'):
            if path==queue.cycle_lock(cycle) or not re.fullmatch(r'\.pool-cycle-[0-9]{8}00\.lock',path.name):continue
            candidate=m.LOCK_ROOT/m.hashlib.sha256(str(path.resolve()).encode()).hexdigest()
            if candidate.exists():
                info=candidate.stat();other_gates.add((info.st_dev,info.st_ino))
        for descriptor in (proc/'fd').iterdir():
            try:
                info=descriptor.stat()
                text=(proc/'fdinfo'/descriptor.name).read_text()
                if (info.st_dev,info.st_ino)==(expected.st_dev,expected.st_ino) and any(
                        line.startswith('lock:') and 'FLOCK' in line and 'WRITE' in line for line in text.splitlines()):
                    owns_gate=True
                if (info.st_dev,info.st_ino) in other_gates and any(
                        line.startswith('lock:') and 'FLOCK' in line and 'WRITE' in line for line in text.splitlines()):
                    owns_other_cycle=True
            except FileNotFoundError:continue
        return (owns_gate and owns_other_cycle and proc.stat().st_uid==os.getuid() and fields[0]!='Z'
                and b'worker' in command
                and any(v.startswith(b'/') and Path(os.fsdecode(v)).resolve()==queue.root for v in command)
                and any(v.startswith(b'/') and Path(os.fsdecode(v)).resolve()==Path(p['module']).resolve() for v in command))
    except (OSError,ValueError):return False
with m.kernel_guard(queue.worker_lock,blocking=False) as worker_gate:
    with m.exclusive(queue.cycle_lock(cycle),blocking=False) as cycle_gate:
        if cycle_gate is not None and (worker_gate is not None or live_worker()):
            with m.exclusive(queue.state_lock):
                state=queue.load_state()
                entry=state.get('cycles',{}).get(cycle) or {}
                files=list((entry.get('files') or {}).values())
                protected=any(x.get('references') or x.get('legacy_task_reference') or x.get('prefetch_reference')
                    or x.get('status') not in {'ready','failed','cancelled','obsolete'}
                    or (x.get('priority')=='task' and x.get('status') not in {'ready','failed','cancelled','obsolete'}) for x in files)
                if not protected:
                    audit=queue.root/'logs'/('quarantine_'+Path(item['trash']).name+'_'+cycle+'.json')
                    record={'version':3,'source':p['source'],'cycle':cycle,'intent':item,'queue_entry':entry,'status':'started'}
                    m.atomic_json(audit,record)
                    quarantine(item)
                    state.get('cycles',{}).pop(cycle,None)
                    queue.save_state(state)
                    m.atomic_json(audit,dict(record,status='removed'))
                    result={'blocked':False,'removed':True}

print(json.dumps(result))
'''
    payload = dict(module=module_path,source=source,root=settings['root'],item=item)
    command = 'python3 -B -c '+shlex.quote(program)+' '+shlex.quote(json.dumps(payload))
    kwargs = dict(timeout=30)
    if hasattr(client,'allow_retries'):
        kwargs.update(_readonly=False)
    value = json.loads(client.run(command,**kwargs))
    if value.get('blocked'):
        from hpc_transport import HpcCleanupBlockedError
        raise HpcCleanupBlockedError('Forcing worker or task reference is active')
    if value != {'blocked':False,'removed':True}:
        raise RuntimeError('Invalid quarantine response')
    return value
