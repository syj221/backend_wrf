"""One compute-free validation path for new and recovered ordinary tasks."""
from copy import deepcopy
from pathlib import Path

from configuration_profile import normalize as normalize_common
from guidance import RULESET
from namelist_config import catalog, resolve, source_catalog
from schemas import WrfTaskCreate, normalize_task_request, configured_spinup_hours
from scripts.namelist_layout import LAYOUT_VERSION


def execution_capability(runtime=None):
    """Bind the deployed generator contract to verified WRF/WPS binaries."""
    import hashlib
    import json
    source = source_catalog()
    expected = {key: source[key] for key in ("source_hash", "binary_hashes")}
    generator_hashes = {name: hashlib.sha256((Path(__file__).parent / "scripts" / name).read_bytes()).hexdigest()
                        for name in ("apply_namelist_plan.py", "namelist_layout.py", "wrf.sh")}
    verified = bool(runtime and all(runtime.get(key) == value for key, value in expected.items()) and
                    runtime.get("generator_hashes") == generator_hashes)
    version = hashlib.sha256(json.dumps(
        [LAYOUT_VERSION, expected, generator_hashes, runtime], sort_keys=True, default=str,
    ).encode()).hexdigest()
    return {"version": "wrf-recovery-2:" + version, "verified": verified,
            "layout_version": LAYOUT_VERSION}


def validate_ordinary_request(request, runtime=None, *, regenerate=False, original_request=None):
    provided = deepcopy(request if original_request is None else original_request)
    value = deepcopy(request)
    if regenerate:
        # A generated digest is not a user choice; all user overrides survive.
        if not value.get("namelist"):
            source = catalog()
            value["namelist"] = {"schema_version": source["schema_version"],
                                 "source_hash": source["source_hash"], "wps": {}, "input": {}}
        value["namelist"].pop("plan_digest", None)
    payload = WrfTaskCreate.model_validate(value)
    normalized = normalize_common(normalize_task_request(payload.model_dump(mode="json")))
    # Check legacy callers too, without changing their generated output contract.
    checked = resolve(normalized, runtime=runtime, original_request=provided)
    resolved = None
    if payload.namelist is not None or payload.configuration_profile:
        resolved = checked
        if resolved["errors"]:
            raise ValueError("；".join(resolved["errors"]))
        if not resolved["execution_verified"]:
            raise ValueError("无法确认当前 WRF/WPS 构建与参数目录一致；保留配置，请恢复连接或刷新运行能力后重试")
        normalized["namelist"] = {**resolved["namelist"], "plan_digest": resolved["digest"]}
    return {"request": normalized, "namelist_plan": resolved,
            "ruleset": RULESET, "rules": checked["rules"],
            "warnings": [f"时间按 UTC 解释；CPU 运行，spin-up 为 {configured_spinup_hours(normalized)} 小时，垂直层数为 {normalized['vertical_levels']}",
                         "已通过配置规则校验；不代表驱动资料和 HPC 当前可用", "计算将消耗资源，需显式确认"]}
