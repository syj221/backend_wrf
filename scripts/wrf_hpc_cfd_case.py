#!/usr/bin/env python3
"""Create an isolated OpenFOAM case for one managed WRF-to-CFD task."""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
from collections import Counter
from pathlib import Path


def copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"OpenFOAM template directory missing: {source}")
    shutil.copytree(source, target)


def replace_once(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"OpenFOAM template does not contain expected {label}")
    return updated


def write_block_mesh(path: Path, width: float, height: float, z_top: float, background_cell: float) -> None:
    nx = max(2, math.ceil(width / background_cell))
    ny = max(2, math.ceil(height / background_cell))
    nz = max(24, math.ceil(z_top / max(background_cell * 2.0, 8.0)))
    x = width / 2.0
    y = height / 2.0
    path.write_text(
        f"""/* Managed WRF-to-CFD block mesh. */
FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object blockMeshDict;
}}
convertToMeters 1;
vertices
(
    (-{x:.6f} -{y:.6f} -20)
    ( {x:.6f} -{y:.6f} -20)
    ( {x:.6f}  {y:.6f} -20)
    (-{x:.6f}  {y:.6f} -20)
    (-{x:.6f} -{y:.6f} {z_top:.6f})
    ( {x:.6f} -{y:.6f} {z_top:.6f})
    ( {x:.6f}  {y:.6f} {z_top:.6f})
    (-{x:.6f}  {y:.6f} {z_top:.6f})
);
blocks
(
    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 8)
);
edges ();
boundary
(
    west  {{ type patch; faces ((0 3 7 4)); }}
    east  {{ type patch; faces ((1 5 6 2)); }}
    south {{ type patch; faces ((0 4 5 1)); }}
    north {{ type patch; faces ((3 2 6 7)); }}
    top   {{ type patch; faces ((4 7 6 5)); }}
    bottom {{ type wall; faces ((0 1 2 3)); }}
);
mergePatchPairs ();
""",
        encoding="utf-8",
    )


def patch_snappy(path: Path, refinement_level: int, max_cells: int) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, r"maxGlobalCells\s+[0-9]+;", f"maxGlobalCells {max_cells};", "maxGlobalCells")
    text = replace_once(text, r"level\s+\(0\s+0\);", f"level ({refinement_level} {refinement_level});", "terrain refinement level")
    path.write_text(text, encoding="utf-8")


def surface_target(case: Path) -> tuple[Path, str]:
    text = (case / "system" / "snappyHexMeshDict").read_text(encoding="utf-8")
    names = list(dict.fromkeys(re.findall(r"([A-Za-z0-9_.-]+\.stl)\b", text, re.IGNORECASE)))
    if len(names) != 1:
        raise ValueError("OpenFOAM template must reference exactly one STL surface")
    match = re.search(rf"{re.escape(names[0])}\s*\{{[^{{}}]*?name\s+([A-Za-z0-9_.-]+)\s*;", text, re.DOTALL)
    if not match:
        raise ValueError("OpenFOAM template STL surface has no patch name")
    return case / "constant" / "triSurface" / names[0], match.group(1)


def write_flat_prism(path: Path, width: float, height: float) -> None:
    x, y, bottom = width / 2 + 10, height / 2 + 10, -21.0
    vertices = [(-x,-y,0),(x,-y,0),(x,y,0),(-x,y,0),(-x,-y,bottom),(x,-y,bottom),(x,y,bottom),(-x,y,bottom)]
    faces = [(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    lines = ["solid managed_flat_surface"]
    for a,b,c in faces:
        lines.extend([" facet normal 0 0 0", "  outer loop", *["   vertex %.6f %.6f %.6f" % vertices[i] for i in (a,b,c)], "  endloop", " endfacet"])
    lines.append("endsolid managed_flat_surface")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_closed_stl(path: Path) -> int:
    """Reject malformed or open STL geometry before invoking snappyHexMesh."""
    raw = path.read_bytes()
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    if len(raw) >= 84 and 84 + struct.unpack_from("<I", raw, 80)[0] * 50 == len(raw):
        count = struct.unpack_from("<I", raw, 80)[0]
        for index in range(count):
            values = struct.unpack_from("<12fH", raw, 84 + index * 50)
            triangles.append((values[3:6], values[6:9], values[9:12]))
    else:
        text = raw.decode("utf-8", errors="strict")
        vertices = [tuple(map(float, match)) for match in re.findall(
            r"(?im)^\s*vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*$", text
        )]
        if len(vertices) % 3:
            raise ValueError("STL vertex count is not divisible by three")
        triangles = [tuple(vertices[index:index + 3]) for index in range(0, len(vertices), 3)]
    if len(triangles) < 4:
        raise ValueError("STL must contain at least four triangles")
    edges: Counter[tuple[tuple[float, float, float], tuple[float, float, float]]] = Counter()
    for triangle in triangles:
        if len(set(triangle)) != 3 or any(not math.isfinite(value) for vertex in triangle for value in vertex):
            raise ValueError("STL contains a degenerate or non-finite triangle")
        normalized = [tuple(round(value, 7) for value in vertex) for vertex in triangle]
        for left, right in zip(normalized, normalized[1:] + normalized[:1]):
            edges[tuple(sorted((left, right)))] += 1
    invalid = sum(count != 2 for count in edges.values())
    if invalid:
        raise ValueError(f"STL is not a closed two-manifold surface ({invalid} invalid edges)")
    return len(triangles)


def patch_ground_roughness(path: Path, patch_name: str, z0: float) -> None:
    text = path.read_text(encoding="utf-8")
    start = re.search(rf"(?m)^\s*{re.escape(patch_name)}\s*\{{", text)
    if not start:
        raise ValueError(f"0/nut has no surface patch {patch_name}")
    brace = text.find("{", start.start()); depth = 0; end = None
    for index in range(brace, len(text)):
        depth += text[index] == "{"; depth -= text[index] == "}"
        if depth == 0:
            end = index + 1; break
    if end is None:
        raise ValueError("0/nut surface patch is malformed")
    indent = "        "
    block = f"{patch_name}\n    {{\n{indent}type nutkAtmRoughWallFunction;\n{indent}z0 uniform {z0:.9g};\n{indent}value uniform 0;\n    }}"
    path.write_text(text[:start.start()] + block + text[end:], encoding="utf-8")


def patch_control(
    path: Path,
    solver_steps: int,
    solver: str | None = None,
    write_interval: int | None = None,
    max_courant: float | None = None,
) -> None:
    def add_entry(value: str, entry: str) -> str:
        closing = value.rfind("}")
        return value[:closing] + entry + value[closing:] if closing >= 0 else value + entry

    text = path.read_text(encoding="utf-8")
    if solver:
        text = re.sub(r"application\s+[^;]+;", f"application     {solver};", text, count=1)
    text = re.sub(r"startFrom\s+latestTime;", "startFrom       startTime;", text, count=1)
    text = re.sub(r"endTime\s+[^;]+;", f"endTime         {solver_steps};", text, count=1)
    text = re.sub(r"writeInterval\s+[^;]+;", f"writeInterval   {write_interval or solver_steps};", text, count=1)
    if max_courant is not None:
        text = re.sub(r"adjustTimeStep\s+[^;]+;", "adjustTimeStep   true;", text, count=1)
        if "adjustTimeStep" not in text:
            text = add_entry(text, "\n\nadjustTimeStep   true;\n")
        if re.search(r"maxCo\s+[^;]+;", text):
            text = re.sub(r"maxCo\s+[^;]+;", f"maxCo           {max_courant:g};", text, count=1)
        else:
            text = add_entry(text, f"\nmaxCo           {max_courant:g};\n")
    path.write_text(text, encoding="utf-8")


def patch_turbulence(path: Path, solver_family: str) -> None:
    """Make the selected solver family explicit in the copied case."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"\bsimulationType\s+(RAS|LES)\s*;", text)
    if not match:
        raise ValueError("turbulenceProperties lacks an explicit simulationType")
    expected = "LES" if solver_family == "les" else "RAS"
    if expected not in text:
        raise ValueError("OpenFOAM template has no %s turbulence configuration" % expected)
    text = text[:match.start()] + "simulationType %s;" % expected + text[match.end():]
    path.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("case", type=Path)
    parser.add_argument("profiles", type=Path)
    parser.add_argument("--width-m", type=float, required=True)
    parser.add_argument("--height-m", type=float, required=True)
    parser.add_argument("--z-top-m", type=float, required=True)
    parser.add_argument("--surface-refinement-m", type=float, default=1.0)
    parser.add_argument("--solver-steps", type=int, default=2)
    parser.add_argument("--solver-family", choices=("rans", "les"), default="rans")
    parser.add_argument("--solver", default="")
    parser.add_argument("--thermal-mode", choices=("thermal", "neutral"), default="thermal")
    parser.add_argument("--solver-template-id", default="managed-default/v1")
    parser.add_argument("--surface-mode", choices=("auto_flat", "reviewed_terrain"), default="auto_flat")
    parser.add_argument("--surface-stl", type=Path)
    parser.add_argument("--roughness-z0-m", type=float, required=True)
    parser.add_argument("--write-interval", type=int, default=0)
    parser.add_argument("--max-courant", type=float, default=None)
    args = parser.parse_args()
    if args.case.exists():
        raise FileExistsError(f"refusing to overwrite CFD case: {args.case}")
    if not args.profiles.is_dir():
        raise FileNotFoundError(f"WRF profile directory missing: {args.profiles}")
    if args.width_m <= 0 or args.height_m <= 0 or args.z_top_m <= 0:
        raise ValueError("CFD dimensions must be positive")
    if args.surface_refinement_m <= 0 or args.surface_refinement_m > 8:
        raise ValueError("surface refinement must be in (0, 8]")
    if not 1e-6 <= args.roughness_z0_m <= 5:
        raise ValueError("roughness z0 must be in [1e-6, 5] m")
    if args.solver_steps <= 0:
        raise ValueError("solver steps must be positive")
    if args.max_courant is not None and not 0 < args.max_courant <= 1:
        raise ValueError("max Courant number must be in (0, 1]")
    if args.write_interval < 0 or (args.write_interval and args.write_interval > args.solver_steps):
        raise ValueError("write interval must be positive and no longer than the solver window")

    args.case.mkdir(parents=True, exist_ok=False)
    copy_tree(args.template / "0", args.case / "0")
    copy_tree(args.template / "system", args.case / "system")
    constant = args.case / "constant"
    constant.mkdir()
    for name in ("g", "thermophysicalProperties", "transportProperties", "turbulenceProperties"):
        shutil.copy2(args.template / "constant" / name, constant / name)
    patch_turbulence(constant / "turbulenceProperties", args.solver_family)
    copy_tree(args.template / "constant" / "triSurface", constant / "triSurface")
    extended = args.template / "constant" / "extendedFeatureEdgeMesh"
    if extended.is_dir():
        copy_tree(extended, constant / "extendedFeatureEdgeMesh")
    shutil.copytree(args.profiles, constant / "boundaryData" / "wrf")
    target_surface, surface_patch = surface_target(args.case)
    for candidate in (constant / "triSurface").glob("*.stl"):
        if candidate != target_surface:
            candidate.unlink()
    if args.surface_mode == "auto_flat":
        write_flat_prism(target_surface, args.width_m, args.height_m)
    else:
        if not args.surface_stl or not args.surface_stl.is_file():
            raise FileNotFoundError("reviewed terrain STL is missing")
        shutil.copy2(args.surface_stl, target_surface)
    surface_triangles = validate_closed_stl(target_surface)
    patch_ground_roughness(args.case / "0" / "nut", surface_patch, args.roughness_z0_m)

    background_cell = max(4.0, args.surface_refinement_m * 4.0)
    refinement_level = max(0, round(math.log(background_cell / args.surface_refinement_m, 2)))
    write_block_mesh(args.case / "system" / "blockMeshDict", args.width_m, args.height_m, args.z_top_m, background_cell)
    patch_snappy(args.case / "system" / "snappyHexMeshDict", refinement_level, 30_000_000)
    end_time = max(1, args.solver_steps)
    patch_control(args.case / "system" / "controlDict", end_time, args.solver or None, args.write_interval or end_time, args.max_courant)
    (constant / "managed_parameterization.json").write_text(
        json.dumps({
            "solver_family": args.solver_family,
            "thermal_mode": args.thermal_mode,
            "solver_template_id": args.solver_template_id,
            "solver": args.solver or None,
            "surface_refinement_m": args.surface_refinement_m,
            "surface_mode": args.surface_mode,
            "surface_patch": surface_patch,
            "roughness_z0_m": args.roughness_z0_m,
            "surface_triangles": surface_triangles,
            "end_time": end_time,
            "write_interval": args.write_interval or end_time,
            "max_courant": args.max_courant,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"prepared case={args.case} background_cell_m={background_cell:g} refinement_level={refinement_level} thermal_mode={args.thermal_mode} solver_template={args.solver_template_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
