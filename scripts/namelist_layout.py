"""Shared, dependency-free layout contract for generated ordinary ARW files."""
import re

LAYOUT_VERSION = "wrf-namelist-layout-2"
GROUP_ORDER = {
    "wps": ("share", "geogrid", "ungrib", "metgrid"),
    "input": ("time_control", "domains", "physics", "noah_mp", "fdda",
              "dynamics", "bdy_control", "grib2", "diags", "afwa", "stoch",
              "pio_control", "namelist_quilt"),
}
REQUIRED = {"wps": GROUP_ORDER["wps"],
            "input": ("time_control", "domains", "physics", "dynamics", "bdy_control", "namelist_quilt")}


def ordered_groups(phase, values):
    return [name for name in GROUP_ORDER[phase] if name in values] + sorted(
        set(values) - set(GROUP_ORDER[phase]))


def validate_layout(text, phase):
    """Check order and delimiters without changing a frozen preview.

    WPS reads share then geogrid/ungrib/metgrid on a forward-only unit.
    A successful Python namelist parse alone does not check this constraint.
    Strings/comments are masked so resource paths cannot look like groups.
    """
    clean = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|![^\n]*",
                   lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    headers = list(re.finditer(r"(?im)^\s*&([a-z]\w*)\b", clean))
    names = [m.group(1).lower() for m in headers]
    reason = ""
    if len(names) != len(set(names)):
        reason = "配置段重复"
    elif set(REQUIRED[phase]) - set(names):
        reason = "缺少必要配置段: " + ", ".join(sorted(set(REQUIRED[phase]) - set(names)))
    elif names != ordered_groups(phase, names):
        reason = "配置段顺序不符合运行读取约定"
    else:
        for i, header in enumerate(headers):
            end = headers[i+1].start() if i+1 < len(headers) else len(clean)
            if clean[header.end():end].count("/") != 1:
                reason = "配置段结束符缺失或重复"
                break
    if reason:
        raise ValueError("NAMELIST_LAYOUT_ERROR: 系统生成配置无效（" + reason +
                         "）；请重新生成、校验并确认计划，禁止直接重试旧配置")


def is_namelist_read_failure(text):
    value = str(text).lower()
    return ("namelist_layout_error" in value or
            ("namelist" in value and "end of file" in value and
             ("finding group" in value or "fio-f-228" in value)))
