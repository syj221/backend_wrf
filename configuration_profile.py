"""Shared ordinary-ARW form contract; the source catalogue is not a questionnaire."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from schemas import (PhysicsConfig, PHYSICS_SUPPORTED_SCHEMES, configured_spinup_hours,
                     configured_vertical_levels, LEGACY_CONFIGURATION_PROFILE,
                     CURRENT_CONFIGURATION_PROFILE)

PROFILE = CURRENT_CONFIGURATION_PROFILE
LEGACY_PROFILE = LEGACY_CONFIGURATION_PROFILE
GROUPS = (("forcing", "边界资料", []), ("time", "模拟时段", ["forcing"]),
          ("domains", "投影与嵌套计算域", ["time"]), ("output", "结果输出", []),
          ("physics", "物理方案", ["domains"]), ("assimilation", "FDDA 网格松弛", ["domains"]))
PHYSICS_LABELS = {"mp_physics": "微物理", "cu_physics": "积云对流", "ra_lw_physics": "长波辐射",
    "ra_sw_physics": "短波辐射", "bl_pbl_physics": "边界层", "sf_sfclay_physics": "近地层",
    "sf_surface_physics": "陆面过程", "sf_urban_physics": "城市冠层", "num_soil_layers": "土壤层数",
    "num_land_cat": "土地类型数", "radt": "辐射调用间隔（分钟）"}
SCHEME_NAMES = {
    "mp_physics": {1:"Kessler",2:"Purdue Lin",3:"WSM3",4:"WSM5",5:"Ferrier",6:"WSM6",7:"Goddard",8:"Thompson",9:"Milbrandt–Yau",10:"Morrison",13:"SBU-YLin",14:"WDM5",16:"WDM6",18:"NSSL 双矩",28:"Thompson 气溶胶感知",50:"P3 单矩云水",51:"P3 双矩云水",55:"Jensen ISHMAEL"},
    "cu_physics": {0:"关闭",1:"Kain–Fritsch",2:"Betts–Miller–Janjic",3:"Grell–Freitas",5:"Grell 3D",6:"Tiedtke",7:"Zhang–McFarlane",11:"MSKF",14:"New SAS",16:"New Tiedtke",93:"Grell–Devenyi"},
    "ra_lw_physics": {1:"RRTM",3:"CAM",4:"RRTMG",5:"Goddard",7:"FLG",14:"RRTMG-K",24:"Fast RRTMG",31:"Held–Suarez"},
    "ra_sw_physics": {1:"Dudhia",3:"CAM",4:"RRTMG",5:"Goddard",7:"FLG",14:"RRTMG-K",24:"Fast RRTMG",31:"Held–Suarez"},
    "bl_pbl_physics": {0:"关闭",1:"YSU",2:"MYJ",4:"QNSE-EDMF",5:"MYNN 2.5",6:"MYNN 3",7:"ACM2",8:"BouLac",9:"UW",10:"TEMF",11:"Shin–Hong",12:"GBM"},
    "sf_sfclay_physics": {0:"关闭",1:"Revised MM5",2:"Janjic Eta",5:"MYNN",7:"Pleim–Xiu",10:"TEMF",91:"Old MM5"},
    "sf_surface_physics": {0:"关闭",1:"SLAB",2:"Noah",3:"RUC",4:"Noah-MP",5:"CLM4",7:"Pleim–Xiu"},
    "sf_urban_physics": {0:"关闭",1:"单层 UCM",2:"多层 BEP",3:"多层 BEM"},
    "num_soil_layers": {4:"当前受管资料：4 层"}, "num_land_cat": {20:"MODIS",21:"MODIS 含湖泊",24:"USGS",28:"USGS 含湖泊",40:"NLCD2006"},
}
DOC = "https://www2.mmm.ucar.edu/wrf/users/wrf_users_guide/build/html/namelist_variables.html"


def definition(capability_id=None):
    physics = []
    for key, label in PHYSICS_LABELS.items():
        values = [4] if key == "num_soil_layers" else sorted(PHYSICS_SUPPORTED_SCHEMES.get(key, []))
        choices = [{"value": n, "label": f"{SCHEME_NAMES.get(key, {}).get(n, f'WRF 方案 {n}')} ({n})",
                    "description": "需同时满足当前资料和关联方案约束；不是个例准确率保证。", "documentation": DOC} for n in values]
        physics.append({"id": key, "label": label, "type": "integer", "choices": choices,
                        "min": 1 if key == "radt" else 0, "max": 60 if key == "radt" else None})
    return {"id": PROFILE, "capability_id": capability_id,
        "groups": [{"id": key, "label": label, "dependencies": deps, "widget": "domains" if key == "domains" else "time" if key == "time" else "group"} for key, label, deps in GROUPS],
        "physics_fields": physics, "physics_defaults": {**PhysicsConfig().model_dump(exclude_none=True), "cu_physics":3},
        "physics_default_rules": {"cu_physics":{"at_or_below_spacing_m":3000,"fine_value":0,"coarse_value":3,"explicit_values_take_precedence":True}},
        "defaults": {"spinup":{"mode":"custom","hours":6},"vertical_levels":88,"history_interval_minutes":60,
                     "assimilation_scheme":"off","data_source":"gfs","forecast_interval_hours":1,"domains":[]},
        "data_sources": ["gfs", "ecmwf"], "forecast_intervals": {"gfs":[1,3,6],"ecmwf":[3]},
        "history_intervals_minutes": [10,30,60,180,360],
        "assimilation_schemes": [{"value":v,"label":label} for v,label in (("off","关闭"),("fdda_weak","弱网格松弛"),("fdda_standard","标准网格松弛"),("fdda_strong","强网格松弛"))],
        "geometry": {"projections":["lambert","mercator","polar"],"max_domains":4,"nesting_ratios":[3,5],"grid_limits":[10,500],"geog_data_res":["default"],"equal_dx_dy":True,"outer_spacing_limits_m":[1000,50000],"finest_spacing_min_m":1000,
                     "topology":"tree","parent_before_child":True,"siblings_overlap":False,"default_domain":"d01"},
        "runtime_parameters":{"spinup_hours":{"min":0,"alignment":"forecast_interval_hours"},"vertical_levels":{"min":10,"max":500,"shared_by_all_domains":True}},
        "fixed_policy": {"runtime_profile":"cpu","forecast_focus":"general"},
        "default_duration_hours":24, "limitations":["计算域总数最多4个，支持兄弟域和多级嵌套，同父兄弟域不能重叠", "当前普通配置服务限制：最细网格不小于1 km，D01不大于50 km；不是WRF通用硬约束", "FDDA 是spin-up期间的网格松弛，不是观测同化或 WRFDA", "地理资料分辨率仅支持 default", "内部参数由服务默认或实际资料派生，不逐项询问"]}


def validate_geometry(domains):
    if any(type(d.get('dx')) is not int for d in domains):
        raise ValueError('常用计算域网格距必须为整数米')
    if domains and (domains[0].get('dx',0) > 50000 or any(d.get('dx',0) < 1000 for d in domains)):
        raise ValueError('当前普通配置服务要求最细网格不小于1 km、D01不大于50 km；请调整网格距或嵌套层数')


def default_time(interval=1, duration=24):
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start -= timedelta(hours=start.hour % interval)
    return {"start_time":start.isoformat().replace("+00:00","Z"), "end_time":(start+timedelta(hours=duration)).isoformat().replace("+00:00","Z")}


def normalize(request):
    """One authority for exposed aliases; preserve extra legacy overrides."""
    result = deepcopy(request)
    profile = result.get("configuration_profile")
    if profile not in {None, PROFILE, LEGACY_PROFILE}:
        raise ValueError("常用配置合同版本不兼容，请读取当前options；输入已保留")
    if profile is None:
        return result
    source = result.get('data_source','gfs')
    interval = result.get('forecast_interval_hours',1)
    if source not in {'gfs','ecmwf'} or type(interval) is not int or interval not in ([3] if source=='ecmwf' else [1,3,6]):
        raise ValueError('资料及边界间隔不合法；ECMWF只支持3小时')
    output_interval = result.get('history_interval_minutes',60)
    if type(output_interval) is not int or output_interval not in {10,30,60,180,360}:
        raise ValueError('结果输出间隔不在当前合法候选中')
    domains = result.get('domains') or []
    if not isinstance(domains,list) or any(not isinstance(d,dict) for d in domains):
        raise ValueError('计算域须为域参数对象列表')
    validate_geometry(domains)
    explicit_physics = result.get("physics") or {}
    result["physics"] = {**PhysicsConfig().model_dump(exclude_none=True), **explicit_physics}
    if profile == PROFILE:
        result["spinup"] = {"mode":"custom","hours":configured_spinup_hours(result)}
        result["vertical_levels"] = configured_vertical_levels(result)
        if result["spinup"]["hours"] % interval:
            raise ValueError("spin-up 小时数须为边界资料周期的整数倍")
        if result["spinup"]["hours"] == 0 and result.get("assimilation_scheme","off") != "off":
            raise ValueError("spin-up 为 0 时不能启用 spin-up FDDA 网格松弛")
        if "cu_physics" not in explicit_physics and explicit_physics.get("cu_physics_by_domain") is None:
            result["physics"]["cu_physics"] = 3
            if domains:
                result["physics"]["cu_physics_by_domain"] = [0 if d.get("dx",0) <= 3000 else 3 for d in domains]
    allowed = {*PHYSICS_LABELS, "preset", "cu_physics_by_domain", "sf_urban_physics_by_domain"}
    if set(result["physics"]) - allowed:
        raise ValueError("物理配置包含未开放的常用字段；额外配置应通过版本化namelist提供")
    if any(type(result["physics"][k]) is not int for k in PHYSICS_LABELS):
        raise ValueError("常用物理参数必须为整数编号")
    PhysicsConfig.model_validate(result["physics"])
    for key in ('cu_physics','sf_urban_physics'):
        value = result['physics'].get(key+'_by_domain')
        if value is not None and (not isinstance(value,list) or len(value)!=len(result.get('domains',[])) or any(type(v) is not int or v not in PHYSICS_SUPPORTED_SCHEMES[key] for v in value)):
            raise ValueError(f'{key} 逐域方案数量或编号不合法')
    if result.get('start_time') and result.get('end_time'):
        start,end = (datetime.fromisoformat(str(result[k]).replace('Z','+00:00')) for k in ('start_time','end_time'))
        if not start.tzinfo or not end.tzinfo or end<=start or end-start>timedelta(days=30):
            raise ValueError('模拟时段须带时区、结束晚于开始且不超过30天')
        interval = result.get('forecast_interval_hours',1)
        start,end = (v.astimezone(timezone.utc) for v in (start,end))
        if any(v.minute or v.second or v.microsecond or v.hour % interval for v in (start,end)):
            raise ValueError('模拟时段须按边界资料间隔对齐UTC整点')
    if any(d.get("dx") != d.get("dy", d.get("dx")) for d in result.get("domains", [])):
        raise ValueError("常用配置要求 dx 与 dy 相同")
    config = result.get("namelist") or {}
    overrides = config.get("input", {}).get("physics", {})
    for key in PHYSICS_LABELS:
        if key not in overrides or overrides[key] is None:
            continue
        actual = result["physics"].get(key + "_by_domain")
        if actual is None: actual = result["physics"][key]
        value = overrides[key]
        if any(type(v) is not int for v in (value if isinstance(value,list) else [value])):
            raise ValueError(f'{key} 的 namelist 覆盖必须为整数编号')
        equal = value == actual or isinstance(value, list) and not isinstance(actual, list) and all(v == actual for v in value)
        if not equal:
            raise ValueError(f"{key} 同时在物理配置和 namelist 中提供了不同值，请明确选择；原输入已保留")
        del overrides[key]
    output = config.get("input", {}).get("time_control", {})
    if "history_interval" in output:
        value = output["history_interval"]
        vals = value if isinstance(value,list) else [value]
        if any(v != result.get("history_interval_minutes",60) for v in vals):
            raise ValueError("结果输出间隔与 namelist 覆盖冲突，请明确选择")
        del output["history_interval"]
    for key in ("e_vert",):
        value = config.get("input", {}).get("domains", {}).get(key)
        levels = configured_vertical_levels(result)
        if value is not None and any(type(v) is not int or v != levels for v in (value if isinstance(value, list) else [value])):
            raise ValueError(f"垂直层数与 e_vert 覆盖冲突，须全域一致为 {levels}；请明确修改")
    if config:
        result["namelist"] = config
    return result


def groups(request, effective):
    """Values deliberately omit execution-managed and low-level defaults."""
    projection = effective["wps"]["geogrid"]
    values = {
        "forcing": {"data_source":request.get("data_source","gfs"),"forecast_interval_hours":request.get("forecast_interval_hours",1)},
        "time": {**{k:request.get(k) for k in ("start_time","end_time")},"spinup":request.get("spinup",{"mode":"custom","hours":6})},
        "domains": {"center":request.get("center"),"domains":request.get("domains"),
                    "projection":{k:projection[k] for k in ("map_proj","truelat1","truelat2","stand_lon")},"vertical_levels":configured_vertical_levels(request)},
        "output": {"history_interval_minutes":request.get("history_interval_minutes",60)},
        "physics": deepcopy(request.get("physics") or {}),
        "assimilation": {"assimilation_scheme":request.get("assimilation_scheme","off")},
    }
    return [{**g,"value":values[g["id"]]} for g in definition()["groups"]]


def annotate(request, resolved, original=None):
    result = deepcopy(resolved)
    result["confirmation_groups"] = groups(request, resolved["effective"])
    exposed = set(PHYSICS_LABELS)
    extras = {file:{group:{k:v for k,v in fields.items() if not (file=="input" and group=="physics" and k in exposed) and not (file=="wps" and group=="geogrid" and k in {"map_proj","truelat1","truelat2","stand_lon"})} for group,fields in (request.get("namelist") or {}).get(file,{}).items()} for file in ("wps","input")}
    original = request if original is None else original
    sources = {group["id"]:{key:("request" if key in original else "service_default") for key in group["value"]} for group in groups(request,resolved["effective"]) if group["id"] not in {"physics","domains"}}
    sources["physics"] = {key:"request" if key in (original.get("physics") or {}) else "service_default" for key in PHYSICS_LABELS}
    sources["domains"] = {key:"request" if key in original else "unconfirmed" for key in ("center","domains")}
    sources["domains"]["vertical_levels"] = "request" if "vertical_levels" in original else "service_default"
    sources["spinup_detail"] = {key:"request" if key in (original.get("spinup") or {}) else "service_default" for key in ("mode","hours")}
    sources["center_detail"] = {key:"request" if key in (original.get("center") or {}) else "derived" for key in (request.get("center") or {})}
    original_domains = original.get("domains") or []
    sources["domain_details"] = [{key:"request" if i < len(original_domains) and key in original_domains[i] else "derived" for key in domain}
                                 for i,domain in enumerate(request.get("domains") or [])]
    sources["physics_by_domain"] = {key:["request" if key+"_by_domain" in (original.get("physics") or {})
                                                  or key in (original.get("physics") or {}) else "service_default"
                                              for _ in (request.get("domains") or [])]
                                    for key in ("cu_physics","sf_urban_physics")}
    sources["projection"] = {key:"request_override" if key in (original.get("namelist") or {}).get("wps",{}).get("geogrid",{}) else "derived_from_center" for key in ("map_proj","truelat1","truelat2","stand_lon")}
    profile = request.get("configuration_profile")
    policy = definition()["fixed_policy"] if profile == PROFILE else {"runtime_profile":"cpu","spinup_hours":6,"vertical_levels":88,"forecast_focus":"general"}
    result["configuration"] = {"profile_id":profile,"fixed_policy":policy,
        "extra_overrides":extras, "parameter_sources":sources,
        "origins":{"common":"request","internal":"service_default","input_dimensions":"runtime_derived"}}
    return result
