"""Read installed source metadata without running WPS/WRF or writing files."""
import hashlib
import json
from pathlib import Path
import sys


def snapshot(wrf, wps, service=None):
    wrf, wps = Path(wrf), Path(wps)
    def read(path):
        return path.read_text(errors="replace") if path.is_file() else ""
    registry = {}
    for path in sorted((wrf / "Registry").glob("*")):
        if path.is_file() and path.suffix != ".var":
            lines = [line for line in read(path).splitlines() if line.split(maxsplit=1)[:1] == ["rconfig"]]
            if lines:
                registry[path.name] = "\n".join(lines)
    sources = {name: read(wps / name) for name in (
        "geogrid/src/gridinfo_module.F", "metgrid/src/gridinfo_module.F",
        "ungrib/src/read_namelist.F", "namelist.wps.all_options")}
    statements = read(wrf / "inc/namelist_statements.inc")
    version = read(wrf / "inc/version_decl")
    builds = {}
    for name, root, binary in (("wrf", wrf, "main/wrf.exe"), ("real", wrf, "main/real.exe"), ("geogrid", wps, "geogrid.exe"), ("ungrib", wps, "ungrib.exe"), ("metgrid", wps, "metgrid.exe")):
        target = root / binary
        digest = hashlib.sha256()
        if target.is_file():
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            builds[name] = digest.hexdigest()
    result = {"registry": registry, "compiled_statements": statements, "wrf_version": version,
            "wps_version": (read(wps / "README").splitlines() or [""])[0], "wps_sources": sources,
            "binary_hashes": builds,
            "source_hash": hashlib.sha256(json.dumps([registry, statements, sources], sort_keys=True).encode()).hexdigest()}
    if service is not None:
        result["generator_hashes"] = {
            name: hashlib.sha256((Path(service) / name).read_bytes()).hexdigest()
            for name in ("apply_namelist_plan.py", "namelist_layout.py", "wrf.sh")
            if (Path(service) / name).is_file()
        }
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)))
