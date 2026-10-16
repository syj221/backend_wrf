"""Read-only readiness of the isolated ordinary WRF release.

The public projection contains codes only. Paths, command output and dependency
details remain in the operator report, outside model-visible tool responses.
"""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import shlex
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = (
    "wrf.sh", "wrf_hpc_gfs.sh", "wrf_storage_guard.sh",
    "download_gfs_00z.sh", "download_ecmwf_00z.sh", "ecmwf_to_wps.filter",
    "apply_namelist_plan.py", "namelist_layout.py", "namelist_source_probe.py",
    "managed_process.py", "validate_forcing.py", "validate_outputs.py", "output_schedule.py",
    "forcing_pool_queue.py", "validate_geography.py",
)
# Frozen releases through 04 predate the optional output schedule module and
# geography contract. Their signed manifest remains their compatibility bound.
LEGACY_SCRIPTS = tuple(name for name in SCRIPTS if name not in {'output_schedule.py', 'validate_geography.py'})
_lock = threading.RLock()
_cache: dict[tuple, tuple[float, dict]] = {}

PROBE = r'''
import hashlib, json, os, subprocess, sys, importlib.metadata, importlib.util
from pathlib import Path
release, data = Path(sys.argv[1]), Path(sys.argv[2])
checks = []
def check(code, ok, detail=''):
    checks.append({'code': code, 'ok': bool(ok), 'detail': str(detail)[:1000]})
try:
    manifest = json.loads((release/'manifest.json').read_text())
    check('release_manifest', manifest.get('schema') == 'wrf-runtime-release-1')
    signed = dict(manifest); fingerprint = signed.pop('fingerprint', None)
    check('manifest_fingerprint', hashlib.sha256(json.dumps(signed,sort_keys=True).encode()).hexdigest() == fingerprint)
    validation=signed.pop('validation',{})
    gates={'release_files','fixed_dependencies','python_environment','build_contract','binary_dependencies','runtime_tables','geography'}
    check('release_validation',validation.get('schema')=='wrf-release-validation-1' and validation.get('complete') is True
          and set(validation.get('checks',[]))==gates
          and validation.get('content_sha256')==hashlib.sha256(json.dumps(signed,sort_keys=True).encode()).hexdigest())
    expected=json.loads(sys.argv[3])
    check('build_contract', all(manifest.get('build',{}).get(k)==v for k,v in expected.items()))
except Exception as exc:
    manifest = {}; check('release_manifest', False, exc)
for name, digest in manifest.get('files', {}).items():
    p = release/name
    ok = p.is_file() and not p.is_symlink() and '..' not in Path(name).parts and not Path(name).is_absolute()
    if ok:
        h = hashlib.sha256()
        with p.open('rb') as f:
            for chunk in iter(lambda:f.read(1048576), b''): h.update(chunk)
        ok = h.hexdigest() == digest
    check('release_file:'+name, ok)
for name,digest in manifest.get('dependencies',{}).items():
    p=Path(name);h=hashlib.sha256()
    if p.is_file():
        with p.open('rb') as f:
            for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    check('fixed_dependency',p.is_file() and h.hexdigest()==digest,name)
for name,version in manifest.get('python_packages',{}).items():
    try: actual=importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError: actual=None
    check('fixed_python_package',actual==version,name)
actual_packages={d.metadata['Name']:d.version for d in importlib.metadata.distributions()}
check('python_package_set',actual_packages==manifest.get('python_packages',{}))
programs = ('wrf/main/wrf.exe','wrf/main/real.exe','wps/geogrid.exe','wps/ungrib.exe','wps/metgrid.exe')
for name in programs:
    p = release/name
    ok = p.is_file() and os.access(str(p), os.X_OK)
    if ok:
        proc = subprocess.Popen(['ldd',str(p)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.communicate()[0].decode('utf8','replace')
        ok = proc.returncode == 0 and 'not found' not in out
    check('binary:'+name, ok)
for name in ('wps/ungrib/Variable_Tables/Vtable.GFS','wps/geogrid/GEOGRID.TBL','wps/metgrid/METGRID.TBL','wrf/run/LANDUSE.TBL'):
    check('runtime_table:'+name, (release/name).is_file())
check('eccodes_tool',bool(os.environ.get('WRF_GRIB_FILTER')) and os.access(os.environ.get('WRF_GRIB_FILTER',''),os.X_OK))
for module in ('f90nml','netCDF4','eccodes'):
    try: __import__(module); check('python:'+module, True)
    except Exception as exc: check('python:'+module, False, exc)
geog = Path(manifest.get('geog_path','/nonexistent'))
if 'geography' in manifest:
    try:
        contract=manifest['geography']
        if not isinstance(contract,dict) or contract.get('schema')!='wrf-geography-1':
            raise ValueError('Invalid frozen geography contract')
        helper=release/'scripts/validate_geography.py'
        if helper.is_symlink() or not helper.is_file() or manifest.get('files',{}).get('scripts/validate_geography.py')!=hashlib.sha256(helper.read_bytes()).hexdigest():
            raise ValueError('Geography validator is not pinned by the release')
        spec=importlib.util.spec_from_file_location('_wrf_geography_gate',str(helper))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        actual=module.validate_geography(release/'wps/geogrid/GEOGRID.TBL',geog,contract)
        check('geography',True,actual['digest'])
    except Exception as exc: check('geography',False,exc)
else:
    # Preserve the signed readiness contract of immutable 04/06 releases used
    # solely to revalidate existing outputs; do not rewrite their manifests.
    check('geography', geog.is_dir() and any(geog.glob('*/index')))
check('data_writable', data.is_dir() and os.access(str(data), os.W_OK|os.X_OK))
check('data_isolated_volume', data.exists() and os.stat(str(data)).st_dev != os.stat(str(release)).st_dev)
if data.exists():
    stat = os.statvfs(str(data)); free = stat.f_bavail*stat.f_frsize
    check('disk_capacity', free >= int(manifest.get('minimum_free_bytes', 110*1024**3)), free)
memory = {}
for line in Path('/proc/meminfo').read_text().splitlines():
    key,value=line.split(':',1); memory[key]=int(value.split()[0])*1024
resources={'memory_available_bytes':memory.get('MemAvailable',0),'disk_available_bytes':free if data.exists() else 0,
           'cpu_count':os.cpu_count(),'load_average':os.getloadavg()[0]}
print(json.dumps({'checks':checks,'resources':resources,'manifest':{k:manifest.get(k) for k in ('release_id','fingerprint','schema')}}))
'''


def readiness(client, settings, *, force=False, operator=False):
    key = (settings.hpc_release_root, settings.hpc_runtime_release, settings.hpc_workbench_data_root)
    with _lock:
        cached = _cache.get(key)
        if not force and cached and time.monotonic() - cached[0] < settings.readiness_cache_seconds:
            report = cached[1]
        else:
            checks = []
            for name in ("fastapi", "pydantic", "f90nml", "netCDF4", "numpy", "Pillow", "pyproj", "pexpect"):
                try:
                    version = importlib.metadata.version(name)
                    checks.append({"code": "local_python:" + name, "ok": True, "detail": version})
                except importlib.metadata.PackageNotFoundError:
                    checks.append({"code": "local_python:" + name, "ok": False})
            for line in (Path(__file__).parent/'requirements.lock').read_text().splitlines():
                if '==' not in line or line.startswith('#'): continue
                name, expected_version = line.split('==',1)
                try: actual_version = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError: actual_version = None
                checks.append({'code':'local_lock:'+name,'ok':actual_version==expected_version})
            configured = all(key)
            checks.append({"code": "isolated_release_configured", "ok": configured})
            manifest = {}
            resources = {}
            if configured:
                try:
                    root = str(Path(settings.hpc_release_root) / "releases" / settings.hpc_runtime_release)
                    command = ". " + shlex.quote(root + "/runtime.env") + " >/dev/null 2>&1 && "
                    command += shlex.quote(settings.hpc_runtime_python) + " -B -c " + shlex.quote(PROBE)
                    command += " " + shlex.quote(root) + " " + shlex.quote(settings.hpc_workbench_data_root)
                    from namelist_config import source_catalog
                    source = source_catalog()
                    expected={k:source[k] for k in ('source_hash','binary_hashes')}
                    expected['generator_hashes']={name:hashlib.sha256((Path(__file__).parent/'scripts'/name).read_bytes()).hexdigest()
                                                  for name in ('apply_namelist_plan.py','namelist_layout.py','wrf.sh')}
                    command += " " + shlex.quote(json.dumps(expected))
                    remote = json.loads(client.run(command, timeout=120))
                    checks.extend(remote["checks"])
                    manifest = remote.get("manifest", {})
                    resources = remote.get("resources", {})
                except Exception as exc:
                    checks.append({"code": "remote_readiness", "ok": False, "detail": str(exc)[:1000]})
            report = {"schema": "wrf-readiness-1", "status": "ready" if all(c["ok"] for c in checks) else "blocked",
                      "checked_at": datetime.now(timezone.utc).isoformat(),
                      "release_id": settings.hpc_runtime_release or None,
                      "fingerprint": manifest.get("fingerprint"), "checks": checks, "resources": resources}
            _cache[key] = (time.monotonic(), report)
    if operator:
        return report
    issues = [{"code": c["code"].split(":", 1)[0], "component": c["code"].split(":", 1)[-1] if c["code"].startswith("local_python:") else None}
              for c in report["checks"] if not c["ok"]]
    return {k: v for k, v in report.items() if k != "checks"} | {"issues": issues}


class MpiConfigurationError(RuntimeError):
    """An actionable request error, rather than an unavailable runtime."""


def mpi_budget(request, configured):
    """Match the pinned runner's conservative fallback and WRF's cell check."""
    import math
    domains = request.get("domains") or []
    side = math.isqrt(configured)
    if side * side < configured:
        side += 1
    ranks = configured if all(min(d["e_we"], d["e_sn"]) >= 10 * side for d in domains) else 1
    fields = (request.get("namelist") or {}).get("input", {}).get("domains", {})
    nx, ny = fields.get("nproc_x", -1), fields.get("nproc_y", -1)
    nx = -1 if nx is None else nx
    ny = -1 if ny is None else ny
    if nx == -1 and ny == -1:
        nx = math.isqrt(ranks)
        while ranks % nx:
            nx -= 1
        ny = ranks // nx
    elif nx > 0 and ny == -1:
        ny = ranks // nx
    elif ny > 0 and nx == -1:
        nx = ranks // ny
    if nx <= 0 or ny <= 0 or nx * ny != ranks:
        raise MpiConfigurationError(f"MPI 分解参数不匹配：配置上限 {configured} 进程，本区域实际使用 {ranks} 进程，"
                           f"nproc_x={fields.get('nproc_x', -1)}、nproc_y={fields.get('nproc_y', -1)}。"
                           "请将高级参数 nproc_x、nproc_y 都设为 -1（自动分解），或设置乘积等于实际进程数的正整数。")
    for index, d in enumerate(domains, 1):
        x, y = d["e_we"] // nx, d["e_sn"] // ny
        if min(x, y) < 10:
            raise MpiConfigurationError(f"D{index:02d} 网格 {d['e_we']}×{d['e_sn']}，MPI {ranks} 进程按 {nx}×{ny} 分解后"
                               f"每块仅 {x}×{y} 格点，WRF 每方向至少需要 10 个格点。"
                               "请将 nproc_x、nproc_y 设为 -1，由系统选择小区域单进程；无需扩大模拟区域。")
    return {"mpi_processes": ranks, "configured_mpi_processes": configured,
            "nproc_x": nx, "nproc_y": ny, "automatic_serial_fallback": ranks != configured}


def compute_budget(request, configured):
    budget = mpi_budget(request, configured)
    cells = sum(int(d['e_we']) * int(d['e_sn']) for d in request.get('domains', [])) * int(request.get('vertical_levels', 88))
    return {**budget, 'memory_bytes': max(2 * 1024**3, cells * 1200),
            'basis': 'conservative_admission_budget_v1'}


def require_ready(client, settings, *, force=True, request=None):
    report = readiness(client, settings, force=force)
    if report["status"] != "ready":
        raise RuntimeError("WRF 运行环境未就绪，请管理员处理：" + ", ".join(sorted({i["code"] for i in report["issues"]})))
    if request:
        domains = request.get("domains", [])
        budget = compute_budget(request, settings.hpc_cpu_mpi_processes)
        ranks = budget["mpi_processes"]
        resources = report.get("resources", {})
        # This is a conservative admission budget, not a measured WRF forecast
        # of RAM usage. Real acceptance records calibrate it per release.
        ram_budget = budget['memory_bytes']
        if settings.ordinary_max_concurrent == 1 and resources.get("memory_available_bytes", 0) < ram_budget:
            raise RuntimeError("当前可用内存不足以满足 WRF 保守资源预算")
        if ranks > resources.get("cpu_count", 0):
            raise RuntimeError("配置的 MPI 进程数超过服务器 CPU 数量")
        report = {**report, "resource_budget": {**budget, "memory_bytes": ram_budget,
                                     "basis": "conservative_admission_budget_v1"}}
    return report
