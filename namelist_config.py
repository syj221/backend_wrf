"""Versioned, typed WPS/WRF configuration and a single namelist generator."""
from copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timedelta
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import re
from scripts.namelist_layout import LAYOUT_VERSION, ordered_groups, validate_layout

SCHEMA = "wrf-namelist-1"
ROOT = Path(__file__).parent
DOC = "https://www2.mmm.ucar.edu/wrf/users/wrf_users_guide/build/html/namelist_variables.html"
SPECIAL = {"chem", "fire", "wrfvarchem", "wrf_cmaq", "namelist_adios2", "ideal", "scm", "tc", "dfi_control", "perturbation"}
INPUT_DEPENDENT = {"input_from_hires", "sst_update", "sf_lake_physics", "sf_ocean_physics", "sf_fire_physics", "chem_opt", "dfi_opt", "grid_sfdda"}
LABELS = {"e_vert": "垂直层数", "p_top_requested": "模式顶气压（Pa）", "time_step": "积分时间步长（秒）",
    "feedback": "双向嵌套反馈", "smooth_option": "嵌套反馈平滑", "map_proj": "地图投影",
    "truelat1": "第一标准纬度", "truelat2": "第二标准纬度", "stand_lon": "投影中央经度",
    "geog_data_res": "地理资料分辨率", "frames_per_outfile": "每个输出文件的时次数",
    "history_interval": "结果输出间隔（分钟）", "restart_interval": "重启文件间隔（分钟）",
    "mp_physics": "微物理方案", "cu_physics": "积云方案", "bl_pbl_physics": "边界层方案",
    "sf_sfclay_physics": "近地层方案", "sf_surface_physics": "陆面方案", "sf_urban_physics": "城市冠层方案",
    "ra_lw_physics": "长波辐射方案", "ra_sw_physics": "短波辐射方案", "radt": "辐射调用间隔（分钟）",
    "num_soil_layers": "土壤层数", "num_land_cat": "土地类型数"}
GEOMETRY = {"max_dom", "e_we", "e_sn", "dx", "dy", "grid_id", "parent_id", "parent_grid_ratio", "i_parent_start", "j_parent_start", "ref_lat", "ref_lon", "ref_x", "ref_y"}
MANAGED = {"wrf_core", "s_we", "s_sn", "s_vert", "num_metgrid_levels", "num_metgrid_soil_levels", "interval_seconds", "input_from_file", "restart", "io_form_history", "io_form_restart", "io_form_input", "io_form_boundary", "io_form_geogrid", "io_form_metgrid", "out_format", "prefix", "fg_name", "specified", "nested", "nocolons"}
DEFAULTS = {"wps.geogrid.map_proj": "lambert", "wps.geogrid.geog_data_res": "default",
    "input.domains.e_vert": 88, "input.domains.p_top_requested": 5000,
    "input.domains.feedback": 1, "input.domains.smooth_option": 0,
    "input.time_control.frames_per_outfile": 1, "input.time_control.restart_interval": 60,
    "input.time_control.adjust_output_times": True}
ENUMS = {"input.domains.feedback": [0,1], "input.domains.smooth_option": [0,1,2],
    "input.dynamics.diff_opt": [0,1,2], "input.dynamics.km_opt": [1,2,3,4,5],
    "input.dynamics.w_damping": [0,1], "input.dynamics.hybrid_opt": [0,2], "input.domains.auto_levels_opt": [1,2],
    "input.fdda.grid_fdda": [0,1,2], "wps.metgrid.process_only_bdy": [0], "wps.ungrib.interp_type": [0,1,2]}


@lru_cache(maxsize=1)
def source_catalog():
    return json.loads((ROOT / "namelist_catalog.json").read_text())


def field_policy(field):
    name, group = field["name"], field["group"]
    if not field["compiled"] or group in SPECIAL or name in {"chem_opt", "sf_fire_physics", "dfi_opt", "sf_ocean_physics"}:
        return "unavailable"
    if name in GEOMETRY or name.startswith(("start_", "end_", "run_")):
        return "derived"
    if name in MANAGED or name.startswith("io_form_") or any(word in name for word in ("path", "filename", "outname", "inname")) or field["type"] == "string" and name not in {"map_proj", "geog_data_res"}:
        return "managed"
    return "editable"


def catalog():
    result = deepcopy(source_catalog())
    from schemas import PHYSICS_SUPPORTED_SCHEMES
    for field in result["fields"]:
        field.update(policy=field_policy(field), label=LABELS.get(field["name"], field["name"]), documentation=DOC)
        field["capability_reason"] = {"unavailable": "当前普通 ARW 执行链未接入此模块或未编译", "managed": "服务管理的资源或文件组织", "derived": "由已确认需求或实际输入资料派生", "editable": "当前编译声明包含此项；额外资料需求在运行准备时核验"}[field["policy"]]
        field["condition"] = "sf_surface_physics=4" if field["group"] == "noah_mp" else "assimilation_scheme != off" if field["group"] == "fdda" else "ordinary_arw"
        if field["file"] == "wps" and field["dimension"] == "array" and field["name"] not in {"fg_name", "constants_name", "new_plvl"}:
            field["dimension"] = "domain"
        if field["name"] in PHYSICS_SUPPORTED_SCHEMES and field["group"] == "physics":
            field["enum"] = sorted(PHYSICS_SUPPORTED_SCHEMES[field["name"]])
        if field["name"] == "map_proj":
            field["enum"] = ["lambert", "mercator", "polar"]
            field["unsupported_options"] = {"lat-lon": "当前计算域接口使用米制网格；角度网格尚未接入"}
        if field["name"] == "geog_data_res":
            field["enum"] = ["default"]
        if field["id"] in DEFAULTS:
            field["default"] = DEFAULTS[field["id"]]
        if field["id"] in ENUMS:
            field["enum"] = ENUMS[field["id"]]
        if field["name"] in {"history_begin_y", "history_end_y"}:
            field["enum"] = [0]
            field["capability_reason"] = "当前结果组织仅支持日、时、分、秒偏移，年份偏移未接入"
    result["capability_id"] = SCHEMA + ":" + result["source_hash"][:16]
    return result


def active(field, values, assimilation):
    group, name = field["group"], field["name"]
    physics = values.get("input", {}).get("physics", {})
    def enabled(key, choice):
        value = physics.get(key, 0)
        return choice in (value if isinstance(value, list) else [value])
    if group == "fdda":
        return assimilation != "off"
    if group == "noah_mp":
        return enabled("sf_surface_physics", 4)
    if name.startswith(("history_interval_", "history_begin_", "history_end_")):
        return name in values.get("input", {}).get("time_control", {})
    if field["file"] == "wps" and name in {"pole_lat", "pole_lon"}:
        return False
    if group in {"stoch", "afwa", "diags", "grib2", "pio_control"}:
        switches = {"stoch": {"stoch_force_opt", "skebs", "sppt", "spp"}, "afwa": {"afwa_diag_opt"}, "diags": {"p_lev_diags", "z_lev_diags"}}.get(group, set())
        if name in switches:
            return True
        return any(any(bool(x) for x in (v if isinstance(v,list) else [v])) for k, v in values.get("input", {}).get(group, {}).items() if k in switches or k.endswith(("opt", "diag", "switch")))
    if name.startswith("frames_per_aux"):
        return False
    if name in {"new_plvl", "interp_type", "pmin"} and field["file"] == "wps":
        return bool(values.get("wps", {}).get("ungrib", {}).get("add_lvls", False))
    if name.startswith("ocean_"):
        return enabled("sf_ocean_physics", 1)
    if name.startswith(("obs_", "obs", "auxinput", "auxhist", "inputout")):
        return False
    return True


def validate_value(field, value, count):
    values = value if isinstance(value, list) else [value]
    if field["dimension"] == "scalar" and isinstance(value, list):
        raise ValueError(f'{field["id"]} 只能填写单值')
    if field["dimension"] == "domain" and isinstance(value, list) and len(value) != count:
        raise ValueError(f'{field["id"]} 需为 {count} 个逐域值')
    if not values or len(values) > 1000:
        raise ValueError("参数数组不能为空或超过 1000 项")
    for item in values:
        typ = field["type"]
        valid = type(item) is bool if typ == "boolean" else type(item) is int if typ == "integer" else type(item) in {int, float} and math.isfinite(item) if typ == "number" else isinstance(item, str) and len(item) <= 256 and not re.search(r"[\n\r\x00]", item)
        if valid and typ == "integer" and not -(2**31) <= item < 2**31:
            raise ValueError(f'{field["id"]} 超出 WRF 32 位整数范围；请填写 -2147483648–2147483647 内的值')
        if not valid or "enum" in field and item not in field["enum"]:
            raise ValueError(f'{field["id"]} 的类型或候选值不合法')
    return values * count if field["dimension"] == "domain" and not isinstance(value, list) else deepcopy(value)


def validate_overrides(config, count):
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA or config.get("source_hash") != source_catalog()["source_hash"]:
        raise ValueError("WRF 参数目录版本已变化，请重新读取配置")
    definitions = {f["id"]: f for f in catalog()["fields"]}
    result = {"schema_version": SCHEMA, "source_hash": config["source_hash"], "wps": {}, "input": {}}
    for file in ("wps", "input"):
        groups = config.get(file, {})
        if not isinstance(groups, dict):
            raise ValueError("namelist 文件分组必须是对象")
        for group, fields in groups.items():
            if not isinstance(fields, dict):
                raise ValueError("namelist 参数分组必须是对象")
            for name, value in fields.items():
                key = f"{file}.{group}.{name}"
                field = definitions.get(key)
                if not field or field["policy"] != "editable":
                    raise ValueError(f"{key} 未开放为用户可设置项；请使用资料或地图配置对应的运行参数")
                result[file].setdefault(group, {})[name] = validate_value(field, value, count) if value is not None else None
    return result


def base_values(request):
    from schemas import configured_spinup_hours, configured_vertical_levels
    domains = request.get("domains") or []
    count = len(domains) or 1
    center = request.get("center") or {"lat": 35, "lon": 105}
    rep = lambda value: [value] * count
    spinup_hours = configured_spinup_hours(request)
    values = {"wps": {"share": {"wrf_core": "ARW", "max_dom": count, "interval_seconds": int(request.get("forecast_interval_hours") or 1) * 3600, "io_form_geogrid": 2},
        "geogrid": {"map_proj": "lambert", "geog_data_res": rep("default"), "ref_lat": center["lat"], "ref_lon": center["lon"], "truelat1": center["lat"], "truelat2": center["lat"], "stand_lon": center["lon"], "geog_data_path": "@GEOG_DATA_PATH@"},
        "ungrib": {"out_format": "WPS", "prefix": "FILE"}, "metgrid": {"fg_name": ["FILE"], "io_form_metgrid": 2}},
        "input": {"time_control": {"interval_seconds": int(request.get("forecast_interval_hours") or 1) * 3600,
            "history_interval": rep(int(request.get("history_interval_minutes") or 60)), "history_begin": rep(spinup_hours*60), "frames_per_outfile": rep(1), "adjust_output_times": True,
            "restart": False, "restart_interval": 60, "io_form_history": 2, "io_form_input": 2, "io_form_restart": 2, "io_form_boundary": 2, "input_from_file": rep(True)},
            "domains": {"max_dom": count, "e_vert": rep(configured_vertical_levels(request)), "p_top_requested": 5000, "feedback": 1, "smooth_option": 0},
            "physics": {}, "dynamics": {"hybrid_opt": 2, "diff_opt": rep(1), "km_opt": rep(4), "non_hydrostatic": rep(True)},
            "bdy_control": {"spec_bdy_width": 5, "specified": [True] + [False] * (count-1), "nested": [False] + [True] * (count-1)},
            # MPI WRF reads this group outside the Registry-generated reader.
            # Use compute ranks for I/O; no dedicated quilting ranks are reserved.
            "namelist_quilt": {"nio_tasks_per_group": 0, "nio_groups": 1, "poll_servers": False}}}
    for name, val in (request.get("physics") or {}).items():
        if name in {"preset", "cu_physics_by_domain", "sf_urban_physics_by_domain"}:
            continue
        definition = next((f for f in source_catalog()["fields"] if f["id"] == "input.physics." + name), {})
        values["input"]["physics"][name] = rep(val) if definition.get("dimension") == "domain" else val
    for name in ("cu_physics", "sf_urban_physics"):
        if request.get("physics", {}).get(name + "_by_domain"):
            values["input"]["physics"][name] = request["physics"][name + "_by_domain"]
    for name in ("e_we", "e_sn", "parent_grid_ratio", "i_parent_start", "j_parent_start"):
        array = [d.get(name, 1) for d in domains]
        values["wps"]["geogrid"][name] = array
        values["input"]["domains"][name] = array
    parents = [d.get("parent_id", index) for index, d in enumerate(domains)] or [0]
    values["wps"]["geogrid"]["parent_id"] = [1] + parents[1:]
    values["input"]["domains"]["parent_id"] = parents
    values["input"]["domains"]["grid_id"] = list(range(1, count+1))
    values["input"]["domains"]["parent_time_step_ratio"] = values["input"]["domains"].get("parent_grid_ratio", rep(1))
    for name in ("dx", "dy"):
        values["wps"]["geogrid"][name] = domains[0][name] if domains else 27000
        values["input"]["domains"][name] = [d[name] for d in domains]
    for name in ("grid_x", "grid_y"):
        if center.get(name) is not None:
            values["wps"]["geogrid"]["ref_" + name[-1]] = center[name]
    if request.get("start_time") and request.get("end_time"):
        start = datetime.fromisoformat(str(request["start_time"]).replace("Z", "+00:00")) - timedelta(hours=spinup_hours)
        end = datetime.fromisoformat(str(request["end_time"]).replace("Z", "+00:00"))
        values["wps"]["share"].update(start_date=rep(start.strftime("%Y-%m-%d_%H:%M:%S")), end_date=rep(end.strftime("%Y-%m-%d_%H:%M:%S")))
        for prefix, time in (("start", start), ("end", end)):
            for part in ("year", "month", "day", "hour", "minute", "second"):
                values["input"]["time_control"][prefix + "_" + part] = rep(getattr(time, part))
        duration = int((end-start).total_seconds())
        for name, value in zip(("run_days", "run_hours", "run_minutes", "run_seconds"), (duration//86400, duration%86400//3600, 0, 0)):
            values["input"]["time_control"][name] = value
    dx = domains[0]["dx"] if domains else 27000
    alignment = math.gcd(int(request.get("forecast_interval_hours") or 1)*3600, int(request.get("history_interval_minutes") or 60)*60)
    values["input"]["domains"]["time_step"] = request.get("time_step_seconds") or next(n for n in range(max(1, dx//1000*5), 0, -1) if alignment % n == 0)
    if request.get("assimilation_scheme", "off") != "off":
        strength = {"fdda_weak": 0.0001, "fdda_standard": 0.0003, "fdda_strong": 0.0006}[request["assimilation_scheme"]]
        values["input"]["fdda"] = {"grid_fdda": [1 if d["dx"] >= 9000 else 0 for d in domains], "gfdda_inname": "wrffdda_d<domain>", "gfdda_end_h": rep(spinup_hours), "gfdda_interval_m": rep(int(request.get("forecast_interval_hours") or 1)*60), "io_form_gfdda": 2, "guv": rep(strength), "gt": rep(strength), "gq": rep(0.0)}
    return values


def resolve(request, *, runtime=None, strict=True, original_request=None):
    import f90nml
    from configuration_profile import PROFILE, LEGACY_PROFILE, normalize, annotate
    from schemas import WrfTaskCreate, configured_spinup_hours
    original_request = request if original_request is None else original_request
    request = normalize(request)
    if request.get("configuration_profile") == PROFILE:
        # Full resolution is an execution contract, never an incomplete draft.
        check = deepcopy(request)
        check["namelist"] = None
        validated = WrfTaskCreate.model_validate(check)
        request["domains"] = [d.model_dump() for d in validated.domains]
    count = len(request.get("domains") or []) or 1
    config = request.get("namelist") or {"schema_version": SCHEMA, "source_hash": source_catalog()["source_hash"]}
    overrides = validate_overrides(config, count)
    values = base_values(request)
    definitions = catalog()["fields"]
    for file in ("wps", "input"):
        for group, fields in overrides[file].items():
            values[file].setdefault(group, {}).update({k: v for k, v in fields.items() if v is not None})
    # Expand only applicable, ordinary-model defaults; unopened modules remain absent.
    for field in definitions:
        if field["file"] == "input" and field["policy"] == "editable" and active(field, values, request.get("assimilation_scheme", "off")) and field["default"] is not None and field["dimension"] != "array":
            group = values["input"].setdefault(field["group"], {})
            if field["name"] not in group and ("enum" not in field or field["default"] in field["enum"]):
                group[field["name"]] = validate_value(field, field["default"], count)
    for field in definitions:
        if field["file"] == "wps" and field["policy"] == "editable" and active(field, values, request.get("assimilation_scheme", "off")) and field["default"] is not None and field["dimension"] != "array":
            group = values["wps"].setdefault(field["group"], {})
            if field["name"] not in group:
                group[field["name"]] = validate_value(field, field["default"], count)
    errors = []
    for name in INPUT_DEPENDENT:
        val = values["input"].get("physics", {}).get(name, values["input"].get("fdda", {}).get(name, 0))
        if any(v not in {0, False} for v in (val if isinstance(val, list) else [val])):
            errors.append(f"{name} 需要额外资料或未接入执行链，不能在当前环境启用")
    for field in definitions:
        value = values.get(field["file"], {}).get(field["group"], {}).get(field["name"])
        if value is not None and field["policy"] == "editable":
            try:
                validate_value(field, value, count)
            except ValueError as exc:
                errors.append(str(exc))
    domain = values["input"]["domains"]
    if len(set(domain["e_vert"])) != 1 or not 10 <= domain["e_vert"][0] <= 500:
        errors.append("各域垂直层数须相同且为 10–500")
    maximum_step = max(1, (request.get("domains") or [{"dx":27000}])[0]["dx"]//1000*5)
    if not 1 <= domain["time_step"] <= maximum_step:
        errors.append(f"time_step={domain['time_step']} 秒须处于 1–{maximum_step} 秒；请减小积分步长，无需扩大区域")
    elif alignment_invalid(request, domain["time_step"]):
        errors.append(f"time_step={domain['time_step']} 秒未整除资料周期 {int(request.get('forecast_interval_hours') or 1)*3600} 秒或输出周期 {int(request.get('history_interval_minutes') or 60)*60} 秒；请选择两者的共同约数")
    if domain.get("time_step_fract_num",0)!=0 or domain.get("time_step_fract_den",1)<=0:
        errors.append("当前受管输出／资源链使用整数秒积分步长；请设 time_step_fract_num=0、time_step_fract_den 为正整数，或明确调整 time_step")
    from vertical_validation import validate_vertical
    if len(set(domain["e_vert"])) == 1 and 10 <= domain["e_vert"][0] <= 500:
        errors.extend(validate_vertical(domain, values["input"].get("dynamics", {})))
    fdda = values["input"].get("fdda", {})
    for key in ("guv", "gt", "gq"):
        val = fdda.get(key, 0)
        if any(v < 0 for v in (val if isinstance(val,list) else [val])):
            errors.append("FDDA 松弛系数不能为负数")
    if request.get("assimilation_scheme", "off") != "off":
        spinup_hours = configured_spinup_hours(request)
        for i, end_h in enumerate(fdda.get("gfdda_end_h", [spinup_hours]*count)):
            if not 0 <= end_h <= spinup_hours or fdda.get("gfdda_interval_m", [60]*count)[i] != int(request.get("forecast_interval_hours") or 1)*60:
                errors.append("FDDA 须限于所选 spin-up 时段，资料间隔须与驱动资料相同")
    if domain.get("use_adaptive_time_step"):
        errors.append("当前受管 FDDA／输出对齐链路尚未接入自适应时间步长")
    for name in ("truelat1", "truelat2"):
        if not -89 < values["wps"]["geogrid"].get(name, 0) < 89:
            errors.append("投影标准纬度须处于 (-89,89)")
    if not -180 <= values["wps"]["geogrid"].get("stand_lon", 0) <= 180:
        errors.append("投影中央经度须处于 [-180,180]")
    control = values["input"]["time_control"]
    if values["wps"].get("metgrid", {}).get("process_only_bdy", 0) != 0:
        errors.append("当前资料准备链路需要完整 met_em，不能仅处理边界")
    if not all(values["wps"].get("share", {}).get("active_grid", [True] * count)):
        errors.append("当前运行链路需要所有已确认计算域生效")
    if any(v != 4 for v in (values["input"]["physics"].get("num_soil_layers", 4),)):
        errors.append("当前受管地理资料和 Noah/Noah-MP 使用4层土壤")
    for key in ("history_interval", "history_begin", "frames_per_outfile"):
        if any(v < 0 for v in control[key]):
            errors.append(f"{key} 不可为负数")
    if any(v < 1 for v in control["frames_per_outfile"]):
        errors.append("每个输出文件的时次数须大于0")
    for i in range(count):
        try:
            interval, begin, end_offset = output_settings(request, i, values)
            step = domain_time_step(domain, i)
            if interval <= 0 or abs(interval/step-round(interval/step)) > 1e-6:
                errors.append(f"D{i+1:02d} 输出间隔 {interval} 秒须为正数且是本域积分步长 {step:g} 秒的整数倍；请调整输出间隔或 parent_time_step_ratio")
            duration = sum(control.get('run_'+unit,0)*scale for unit,scale in (('days',86400),('hours',3600),('minutes',60),('seconds',1)))
            if begin>min(duration,end_offset):
                errors.append(f"D{i+1:02d} 输出起点 {begin} 秒晚于有效结束 {min(duration,end_offset)} 秒，将无结果；请调整 history_begin／history_end 或模拟时段")
        except (ValueError, TypeError, ZeroDivisionError):
            errors.append("输出组织配置无效")
    physics = values["input"]["physics"]
    from schemas import PBL_SURFACE_COMPATIBILITY
    for i in range(count):
        get = lambda key: physics.get(key, [0]*count)[i] if isinstance(physics.get(key), list) else physics.get(key, 0)
        allowed = PBL_SURFACE_COMPATIBILITY.get(get("bl_pbl_physics"))
        if allowed and get("sf_sfclay_physics") not in allowed:
            errors.append(f"D{i+1:02d} 边界层 bl_pbl_physics={get('bl_pbl_physics')} 与近地层 sf_sfclay_physics={get('sf_sfclay_physics')} 不兼容；该边界层可配近地层 {sorted(allowed)}，请明确选择")
        if get("sf_surface_physics") == 4 and get("num_land_cat") != 21:
            errors.append("Noah-MP 要求土地类型数为 21")
        if get("sf_urban_physics") and get("sf_surface_physics") not in {2, 4}:
            errors.append("当前城市冠层链路需要 Noah 或 Noah-MP 陆面")
    from configuration_checks import combination_errors
    errors.extend(combination_errors(values, count))
    from config import settings
    from runtime_release import compute_budget, MpiConfigurationError
    resource_budget = None
    try:
        resource_budget = compute_budget(request, settings.hpc_cpu_mpi_processes)
    except MpiConfigurationError as exc:
        errors.append(str(exc))
    if errors and strict:
        raise ValueError("；".join(dict.fromkeys(errors)))
    pending = []
    for file in ("wps", "input"):
        for group, fields in values[file].items():
            for name in list(fields):
                definition = next((f for f in definitions if f["id"] == f"{file}.{group}.{name}"), None)
                if definition and definition["policy"] == "editable" and not active(definition, values, request.get("assimilation_scheme", "off")):
                    del fields[name]
    for field in definitions:
        if field["policy"] != "editable" or not active(field, values, request.get("assimilation_scheme", "off")):
            continue
        value = values.get(field["file"], {}).get(field["group"], {}).get(field["name"])
        pending.append({**field, "value": value})
    previews = {}
    for file in ("wps", "input"):
        stream = io.StringIO()
        # Plain dicts are sorted by f90nml, breaking WPS sequential reads.
        groups = OrderedDict((name, values[file][name]) for name in ordered_groups(file, values[file]))
        f90nml.Namelist(groups).write(stream)
        content = stream.getvalue()
        validate_layout(content, file)
        def semantic(value):
            if isinstance(value, dict):
                return {k: semantic(v) for k, v in value.items()}
            if isinstance(value, list):
                return semantic(value[0]) if len(value) == 1 else [semantic(v) for v in value]
            return value
        if semantic(dict(f90nml.reads(content))) != semantic(dict(f90nml.Namelist(values[file]))):
            raise ValueError("namelist 序列化回读校验失败")
        previews[file] = content
    digest = hashlib.sha256(json.dumps([values, source_catalog()["source_hash"], LAYOUT_VERSION], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if strict and config.get("plan_digest") and config["plan_digest"] != digest:
        raise ValueError("规范化配置与已冻结计划摘要不一致，请重新确认计划")
    verified = bool(runtime and runtime.get("source_hash") == source_catalog()["source_hash"] and runtime.get("binary_hashes") == source_catalog()["binary_hashes"])
    result = {"namelist": overrides, "effective": values, "previews": previews, "required_confirmation": pending, "errors": list(dict.fromkeys(errors)),
            "digest": digest, "capability_id": catalog()["capability_id"], "execution_verified": verified,
            "runtime_derived": ["input.domains.num_metgrid_levels", "input.domains.num_metgrid_soil_levels"],
            "runtime": {"verified": verified, "wrf_version": source_catalog()["wrf_version"], "wps_version": source_catalog()["wps_version"]}}
    from guidance import configuration_rules
    result["rules"] = configuration_rules(request, effective=values)
    result["resource_budget"] = resource_budget
    return annotate(request, result, original_request) if request.get("configuration_profile") in {PROFILE, LEGACY_PROFILE} else result


def domain_time_step(domain, index):
    """Follow ancestors rather than multiplying ratios of unrelated siblings."""
    parents = domain["parent_id"]
    ratios = domain["parent_time_step_ratio"]
    if ratios[0] != 1 or parents[0] != 0:
        raise ValueError("D01 的 parent_id/parent_time_step_ratio 须为 0/1")
    step = domain["time_step"]
    while index:
        parent, ratio = parents[index], ratios[index]
        if type(parent) is not int or not 1 <= parent <= index or type(ratio) is not int or ratio < 1:
            raise ValueError("父域和时间步比例须形成父先子后的有效树")
        step /= ratio
        index = parent - 1
    return step


def alignment_invalid(request, timestep):
    return type(timestep) is not int or timestep < 1 or int(request.get("forecast_interval_hours") or 1)*3600 % timestep or int(request.get("history_interval_minutes") or 60)*60 % timestep


def output_settings(request, index, values=None):
    from schemas import configured_spinup_hours
    from scripts.output_schedule import history_settings
    control = values.get("input", {}).get("time_control", {}) if values else (request.get("namelist") or {}).get("input", {}).get("time_control", {})
    return history_settings(control, index, request.get("history_interval_minutes") or 60,
                            configured_spinup_hours(request) * 60)
