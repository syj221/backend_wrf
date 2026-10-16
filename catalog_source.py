"""Build a parameter catalogue from Registry and compiled namelist declarations."""
import re
import shlex
import math


def build_catalog(snapshot):
    compiled = {(g.lower(), n.lower()) for g, n in re.findall(r"NAMELIST\s*/(\w+)/\s*(\w+)", snapshot["compiled_statements"], re.I)}
    fields = {}
    for filename in sorted(snapshot["registry"], key=lambda n: (n != "Registry.EM_COMMON", n.startswith("Registry.CONVERT"), n)):
        text = snapshot["registry"][filename]
        for line in text.splitlines():
            try:
                row = shlex.split(line, comments=True)
            except ValueError:
                continue
            if len(row) < 6 or not row[3].startswith("namelist,"):
                continue
            _, kind, name, group, dimension, default, *_ = row
            group = group.split(",")[1].lower()
            kind, name, dimension = kind.lower(), name.lower(), dimension.lower()
            key = f"input.{group}.{name}"
            if key in fields:
                continue
            typ = "boolean" if kind == "logical" else "integer" if kind == "integer" else "number" if kind.startswith(("real", "double")) else "string"
            value = default
            try:
                value = default.lower() == ".true." if typ == "boolean" else int(default) if typ == "integer" else float(default.replace("D", "e").replace("d", "e")) if typ == "number" else default
            except ValueError:
                value = None
            if isinstance(value, float) and not math.isfinite(value):
                value = None
            fields[key] = {"id": key, "file": "input", "group": group, "name": name, "type": typ,
                "dimension": "domain" if dimension == "max_domains" else "scalar" if dimension == "1" else "array",
                "size_expression": dimension, "default": value,
                "description": row[8] if len(row) > 8 else name, "units": row[9] if len(row) > 9 else "",
                "compiled": (group, name) in compiled, "source": f"Registry/{filename}"}
    # WPS declares its catalogue in Fortran, including options absent from examples.
    for filename, text in snapshot["wps_sources"].items():
        if not filename.endswith(".F"):
            continue
        logical = re.sub(r"!.*", "", text)
        logical = re.sub(r"&\s*\n\s*&?", " ", logical)
        declarations = {}
        for match in re.finditer(r"^\s*(integer|real|logical|character)([^\n]*?)::([^\n]+)", logical, re.I | re.M):
            typ = {"integer": "integer", "real": "number", "logical": "boolean", "character": "string"}[match[1].lower()]
            for token in re.split(r",\s*(?![^()]*\))", match[3]):
                variable = re.match(r"\s*(\w+)\s*(\([^)]*\))?", token)
                if variable:
                    declarations[variable[1].lower()] = (typ, "domain" if variable[2] and "max_dom" in variable[2].lower() else "array" if variable[2] or "dimension" in match[2].lower() else "scalar")
        for match in re.finditer(r"namelist\s*/(\w+)/\s*([^\n]+)", logical, re.I):
            group = match[1].lower()
            for name in re.findall(r"\b\w+\b", match[2]):
                name = name.lower()
                key = f"wps.{group}.{name}"
                typ, dimension = declarations.get(name, ("string", "scalar"))
                default = None
                # Initial assignments in the installed reader are the authority for WPS defaults.
                assignment = re.search(r"^\s*" + re.escape(name) + r"(?:\([^)]*\))?\s*=\s*([^\n!]+)", logical, re.I | re.M)
                if assignment:
                    literal = assignment[1].strip()
                    try:
                        default = literal.lower() == ".true." if typ == "boolean" and literal.lower() in {".true.", ".false."} else int(literal) if typ == "integer" else float(literal.replace("D", "e").replace("d", "e")) if typ == "number" else literal.strip("'\"") if literal.startswith(("'", '"')) else None
                    except ValueError:
                        pass
                fields[key] = {"id": key, "file": "wps", "group": group, "name": name, "type": typ,
                    "dimension": dimension, "default": default, "description": name, "units": "", "compiled": True, "source": filename}
    version = re.search(r"V([\d.]+)", snapshot["wrf_version"])
    wps_version = re.search(r"Version\s+([\d.]+)", snapshot["wps_version"])
    missing = sorted(f"input.{g}.{n}" for g, n in compiled if f"input.{g}.{n}" not in fields)
    if missing:
        raise ValueError("Compiled namelist declarations lack Registry metadata: " + ", ".join(missing))
    return {"schema_version": "wrf-namelist-1", "wrf_version": version[1] if version else "unknown",
            "completeness": {"compiled_wrf_fields": len(compiled), "missing_compiled_fields": missing},
            "wps_version": wps_version[1] if wps_version else "unknown", "source_hash": snapshot["source_hash"],
            "binary_hashes": snapshot["binary_hashes"], "fields": list(fields.values())}
