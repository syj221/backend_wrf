"""Install backend-generated namelists, resolving only managed runtime fields."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from namelist_source_probe import snapshot
from namelist_layout import validate_layout


def apply(config, phase, target):
    plan = config.get("namelist_plan")
    if not plan:
        return
    current = snapshot(os.environ["WRF_SOURCE_DIR"], os.environ["WPS_SOURCE_DIR"])
    if current["source_hash"] != plan["source_hash"] or current["binary_hashes"] != plan["binary_hashes"]:
        raise ValueError("WRF/WPS 构建已变化，需要重新校验并确认运行计划")
    text = plan["previews"][phase]
    if hashlib.sha256(text.encode()).hexdigest() != plan["preview_hashes"][phase]:
        raise ValueError("运行配置预览与冻结计划不一致，停止准备")
    validate_layout(text, phase)
    if phase == "wps":
        path = os.environ["GEOG_DATA_PATH"]
        if any(char in path for char in "'\n\r"):
            raise ValueError("地理资料路径无法写入 namelist")
        text = text.replace("@GEOG_DATA_PATH@", path)
    else:
        dimensions = {}
        for field, env in (("num_metgrid_levels", "WRF_NUM_METGRID_LEVELS"), ("num_metgrid_soil_levels", "WRF_NUM_METGRID_SOIL_LEVELS")):
            value = os.environ.get(env, "")
            if not value.isdigit() or int(value) < 1:
                raise ValueError("实际 met_em 输入维度尚未核验，不能生成运行配置")
            dimensions[field] = int(value)
        insertion = "\n".join(f"    {key} = {value}" for key, value in dimensions.items())
        text = re.sub(r"(?im)^&domains\s*$", "&domains\n" + insertion, text, count=1)
    validate_layout(text, phase)
    target = Path(target)
    target.write_text(text, encoding="utf-8")
    audit = {"plan_digest": plan["digest"], "file": target.name,
             "sha256": hashlib.sha256(text.encode()).hexdigest(), "runtime_derived": dimensions if phase == "input" else {}}
    target.with_suffix(target.suffix + ".plan.json").write_text(json.dumps(audit), encoding="utf-8")


if __name__ == "__main__":
    try:
        apply(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2], sys.argv[3])
    except (ValueError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc
