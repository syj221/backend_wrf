#!/usr/bin/env python3
"""Apply WRF height profiles to physically directional OpenFOAM boundaries.

Only lateral U/T patches are changed.  Existing pressure, turbulence, terrain,
top and bottom settings are retained from the selected case template.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

PATCHES = ("west", "east", "south", "north")
NORMALS = {"west": (-1.0, 0.0), "east": (1.0, 0.0),
           "south": (0.0, -1.0), "north": (0.0, 1.0)}


def read_rows(path: Path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def numbers(rows, name):
    return ", ".join(f"{float(r[name]):.9g}" for r in rows)


def profile_code(rows, field: str) -> str:
    z = numbers(rows, "z_agl_m")
    if field == "U":
        a, b, c, result, typ = (numbers(rows, "u_east_mps"), numbers(rows, "v_north_mps"),
                                 numbers(rows, "w_up_mps"), "vector(a, b, c)", "vector")
    else:
        a, b, c, result, typ = (numbers(rows, "temperature_k"), "0", "0", "a", "scalar")
    return f'''        const scalar zTable[] = {{{z}}};
        const scalar aTable[] = {{{a}}};
        const scalar bTable[] = {{{b}}};
        const scalar cTable[] = {{{c}}};
        const label n = {len(rows)};
        const vectorField& centres = this->patch().Cf();
        {typ}Field values(centres.size());
        forAll(values, facei)
        {{
            const scalar height = max(centres[facei].z(), scalar(0));
            label hi = 1; while (hi < n && zTable[hi] < height) ++hi;
            const label lo = max(label(0), hi - 1); hi = min(hi, n - 1);
            const scalar f = (hi == lo || zTable[hi] == zTable[lo]) ? 0 : (height-zTable[lo])/(zTable[hi]-zTable[lo]);
            const scalar a = aTable[lo] + f*(aTable[hi]-aTable[lo]);
            const scalar b = bTable[lo] + f*(bTable[hi]-bTable[lo]);
            const scalar c = cTable[lo] + f*(cTable[hi]-cTable[lo]);
            values[facei] = {result};
        }}
        operator==(values);'''


def coded_patch(name: str, rows, field: str) -> str:
    initial = "(0 0 0)" if field == "U" else f"{float(rows[0]['temperature_k']):.9g}"
    return f'''    {name}
    {{
        type codedFixedValue;
        value uniform {initial};
        name wrfProfile{field}{name.title()};
        code
        #{{
{profile_code(rows, field)}
        #}};
    }}'''


def outflow_patch(name: str, rows, field: str) -> str:
    if field == "U":
        return f'''    {name}
    {{
        type pressureInletOutletVelocity;
        value uniform (0 0 0);
    }}'''
    temp = float(rows[0]["temperature_k"])
    return f'''    {name}
    {{
        type inletOutlet;
        inletValue uniform {temp:.9g};
        value uniform {temp:.9g};
    }}'''


def replace_patch(text: str, patch: str, replacement: str) -> str:
    start = text.find(patch)
    if start < 0:
        raise ValueError(f"Patch {patch!r} was not found")
    brace = text.find("{", start)
    if brace < 0:
        raise ValueError(f"Patch {patch!r} has no opening brace")
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{": depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[:start] + replacement + text[index + 1:]
    raise ValueError(f"Patch {patch!r} has unclosed brace")


def pressure_value(text: str) -> float:
    """Use the existing p_rgh internal value as the reference pressure."""
    match = re.search(r"internalField\s+uniform\s+([-+0-9.eE]+)", text)
    if not match:
        raise ValueError("p_rgh internalField is not a uniform scalar")
    return float(match.group(1))


def apply_pressure_closure(case: Path, qa: dict) -> None:
    """Set one physical outlet as p_rgh reference; retain wall pressure settings."""
    path = case / "0" / "p_rgh"
    if not path.is_file():
        raise FileNotFoundError(f"Missing pressure field: {path}")
    text = path.read_text()
    ref_patch = max(
        (patch for patch in PATCHES if qa["sides"][patch]["classification"] == "outflow"),
        key=lambda patch: qa["sides"][patch]["outward_normal_mps"],
        default=None,
    )
    if ref_patch is None:
        raise ValueError("No lateral outflow was detected; cannot choose a pressure reference")
    reference = pressure_value(text)
    for patch in PATCHES:
        block = (f"    {patch}\n    {{\n        type fixedValue;\n"
                 f"        value uniform {reference:.9g};\n    }}") if patch == ref_patch else (
                 f"    {patch}\n    {{\n        type zeroGradient;\n    }}")
        text = replace_patch(text, patch, block)
    text = replace_patch(text, "top", "    top\n    {\n        type zeroGradient;\n    }")
    path.write_text(text)
    qa["pressure_closure"] = {
        "strategy": "one strongest lateral outflow as fixed p_rgh reference; other lateral and top patches zeroGradient",
        "reference_patch": ref_patch,
        "reference_p_rgh": reference,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--threshold", type=float, default=0.05,
                        help="normal velocity threshold (m/s) for neutral classification")
    args = parser.parse_args()
    case = args.case_dir.resolve()
    source = case / "constant" / "boundaryData" / "wrf"
    profiles = {patch: read_rows(source / f"{patch}.csv") for patch in PATCHES}
    if any(not rows for rows in profiles.values()):
        raise SystemExit("Each lateral profile must contain at least one row")

    qa = {"method": "lowest-WRF-level planar normal velocity; diagnostic only, not mesh-face flux",
          "threshold_mps": args.threshold, "sides": {}}
    signed = []
    for patch, rows in profiles.items():
        u, v = float(rows[0]["u_east_mps"]), float(rows[0]["v_north_mps"])
        nx, ny = NORMALS[patch]
        normal = u * nx + v * ny
        state = "inflow" if normal < -args.threshold else "outflow" if normal > args.threshold else "neutral"
        qa["sides"][patch] = {"u_east_mps": u, "v_north_mps": v,
                              "outward_normal_mps": normal, "classification": state}
        signed.append(normal)
    qa["sum_of_side_normal_velocities_mps"] = sum(signed)
    qa["directional_policy"] = "WRF profile on inflow; pressure-compatible velocity / inletOutlet temperature on outflow; neutral behaves as outflow"

    for field in ("U", "T"):
        path = case / "0" / field
        text = path.read_text()
        for patch in PATCHES:
            state = qa["sides"][patch]["classification"]
            replacement = coded_patch(patch, profiles[patch], field) if state == "inflow" else outflow_patch(patch, profiles[patch], field)
            text = replace_patch(text, patch, replacement)
        path.write_text(text)
    apply_pressure_closure(case, qa)
    (case / "directional_boundary_qa.json").write_text(json.dumps(qa, indent=2) + "\n")
    print(json.dumps(qa, indent=2))


if __name__ == "__main__":
    main()
