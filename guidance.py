"""Read-only recommendations and versioned, evidence-labelled WRF checks."""
from datetime import datetime, timedelta, timezone
import math
from pydantic import ValidationError

RULESET = "ordinary-wrf-2026-10-15"
WPS = "https://www2.mmm.ucar.edu/wrf/users/namelist_best_prac_wps.html"
NAMELIST = "https://www2.mmm.ucar.edu/wrf/users/wrf_users_guide/build/html/namelist_variables.html"


def finding(code, severity, fields, message, suggestion, source=WPS, category="official_guidance"):
    return dict(id=code, severity=severity, fields=fields, message=message,
                suggestion=suggestion, source=source, category=category,
                applicability="普通 ARW；构建相关物理选项仍需启动前核对", ruleset=RULESET)


def configuration_rules(request, complete=False, effective=None):
    from schemas import WrfTaskCreate
    result = []
    if complete:
        try:
            WrfTaskCreate.model_validate(request)
        except ValidationError as exc:
            for error in exc.errors():
                fields = [".".join(map(str, error["loc"]))] if error["loc"] else ["request"]
                result.append(finding("service.configuration." + fields[0], "error", fields,
                                      str(error["msg"]), "按提示修改后重新校验", "backend_wrf/schemas.py", "service_constraint"))
    domains = request.get("domains") or []
    for domain in domains:
        if min(domain.get("e_we", 0), domain.get("e_sn", 0)) < 100:
            result.append(finding("wps.domain_size." + domain.get("id", ""), "warning", ["domains"],
                                  "该域少于 100×100 格点，边界影响可能较大", "结合天气过程尺度扩大计算域；这是建议而非硬性限制"))
        if domain.get("dx") != domain.get("dy"):
            result.append(finding("wps.square_grid", "warning", ["domains"],
                                  "投影网格的 dx 与 dy 不同", "检查投影并优先使用相等的网格距"))
    physics = request.get("physics") or {}
    for index, domain in enumerate(domains):
        per_domain = physics.get("cu_physics_by_domain")
        cu = per_domain[index] if isinstance(per_domain, list) and len(per_domain) > index else physics.get("cu_physics")
        if cu is not None and ((domain.get("dx", 0) >= 10000 and cu == 0) or (domain.get("dx", 0) <= 3000 and cu != 0)):
            result.append(finding("physics.convection_scale." + str(index), "warning", ["physics.cu_physics", "domains"],
                                  "积云方案需结合当前网格尺度评估", "核对是否显式解析对流，以及方案是否具有尺度适应性", NAMELIST))
    for code, fields, message, suggestion in (
        ("vertical", ["vertical_levels", "namelist.input.domains.p_top_requested"],
         "待核验：实际驱动资料最高层气压是否覆盖模型顶，地形处的有效气压层是否足够",
         "资料准备后核验 met_em 垂直覆盖、地面气压与温度；不满足时停止初始化，不自动改层数或模型顶"),
        ("forcing", ["data_source", "start_time", "end_time"],
         "待核验：驱动资料时空覆盖、必需气象变量、土壤层与陆面／土地类型是否匹配",
         "准备阶段逐域核对实际文件与字段；缺失资料先补齐或由用户调整配置"),
        ("stability", ["domains", "physics", "time_step_seconds"],
         "待核验：实际地形、地图比例因子、物理方案所需字段及积分稳定性",
         "确定的配置冲突已阻断；实际初始化和 CFL 检查仍需运行证据，不能保证所有合法组合必然成功"),
    ):
        result.append(finding("runtime."+code+"_pending", "pending", fields,
                              message, suggestion, NAMELIST, "input_dependent"))
    resolved_physics = (effective or {}).get("input", {}).get("physics", {})
    for keys, message, suggestion in (
        (("sf_urban_physics", "use_wudapt_lcz"), "城市冠层所需城市类别、形态及参数表",
         "准备后核验所选城市方案对应的 geogrid 字段与表文件"),
        (("sf_surf_irr_scheme",), "灌溉方案所需土地利用及灌溉资料",
         "准备后核验实际地理资料的灌溉字段与覆盖范围"),
        (("qna_update", "use_rap_aero_icbc", "aer_opt"), "气溶胶方案的外部资料与更新时段",
         "准备后核验所需气溶胶输入、辅助通道及时间覆盖"),
        (("couple_farms",), "风电场位置和机组参数资料",
         "准备后核验风电场输入及与短波耦合方案的匹配"),
    ):
        enabled = any(any(bool(v) for v in (resolved_physics.get(k,0) if isinstance(resolved_physics.get(k),list) else [resolved_physics.get(k,0)])) for k in keys)
        if enabled:
            result.append(finding("runtime."+keys[0]+"_pending", "pending", ["namelist.input.physics."+k for k in keys],
                                  "待核验："+message, suggestion, NAMELIST, "input_dependent"))
    return result


def recommendations(request, health, pool=None, now=None):
    from schemas import configured_spinup_hours, configured_vertical_levels
    from gfs import boundary_buffer_hours
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source = str(request.get("data_source") or "gfs").lower()
    interval = 3 if source == "ecmwf" else int(request.get("forecast_interval_hours") or 1)
    aligned = datetime.fromtimestamp(math.ceil(now.timestamp() / (interval * 3600)) * interval * 3600, timezone.utc)
    def parse(value, fallback):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc) if value else fallback
    start = parse(request.get("start_time"), aligned)
    duration = int(request.get("duration_hours") or 24)
    end = parse(request.get("end_time"), start + timedelta(hours=duration))
    spinup_hours = configured_spinup_hours(request)
    levels = configured_vertical_levels(request)
    model_start = start - timedelta(hours=spinup_hours)
    cycle_time = min(model_start.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(hour=0, minute=0, second=0, microsecond=0))
    cycle = cycle_time.strftime("%Y%m%d%H")
    first = math.floor((model_start - cycle_time).total_seconds() / (interval * 3600)) * interval
    forcing_buffer = boundary_buffer_hours(request.get("configuration_profile"))
    last = math.ceil(((end - cycle_time).total_seconds() / 3600 + forcing_buffer) / interval) * interval
    nominal_cycle = cycle
    cycles = [c for item in (pool or {}).get("pool_items", [])
              if item.get("provider") == source for c in item.get("cycles", [])]

    def bounds_for_cycle(key):
        # Mirror the executor's complete-cycle fallback without reserving or downloading files.
        try:
            stamp = datetime.strptime(key, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
        low = math.floor((model_start - stamp).total_seconds() / (interval * 3600)) * interval
        high = math.ceil(((end - stamp).total_seconds() / 3600 + forcing_buffer) / interval) * interval
        if low < 0 or high > 72 or end <= start:
            return None
        return low, high, list(range(low, high + 1, interval))

    def observation(key):
        bounds = bounds_for_cycle(key)
        if bounds is None:
            return {"cycle": key, "status": "unavailable", "missing_hours": None}
        match = next((c for c in cycles if c.get("cycle") == key), None)
        missing = None
        status = "unknown"
        if match:
            available = match.get("available_hours")
            missing = [h for h in bounds[2] if h not in available] if isinstance(available, list) else None
            status = "ready" if missing == [] or (missing is None and match.get("complete")) else "waiting_publish" if match.get("status") == "waiting_publish" else "needs_download"
        elif pool is not None and pool.get("status") != "unavailable":
            status = "needs_download"
        return {"cycle": key, "status": status, "required_hours": list(bounds[:2]), "missing_hours": missing}

    nominal = observation(nominal_cycle)
    # Partial old cycles are not execution fallbacks even when a short subwindow is cached.
    for key in sorted({str(c.get("cycle") or "") for c in cycles if c.get("complete")}, reverse=True):
        if bounds_for_cycle(key) is not None and observation(key)["status"] == "ready":
            cycle = key
            first, last, _ = bounds_for_cycle(key)
            break
    selected = observation(cycle)
    coverage, missing_hours = selected["status"], selected["missing_hours"]
    iso = lambda value: value.isoformat().replace("+00:00", "Z")
    advice = {
        "time": {"value": [iso(start), iso(end)], "reason": f"优先保留用户时段或明确的 {duration} 小时需求；仅未指定时默认 24 小时。所选 {spinup_hours} 小时 spin-up 不计入用户预报时长。", "durations_hours": sorted({duration, 6, 12, 24, 48})},
        "spinup": {"value":{"mode":"custom","hours":spinup_hours},"reason":"默认6小时，可设为0；须按边界资料周期对齐，并结合目标过程评估初始化影响。"},
        "vertical_levels": {"value":levels,"reason":"默认88层，所有域保持一致；层数可在10–500间设置，增加层数会提高计算和存储成本。"},
        "data_source": {"value": source, "reason": "按当前资料源检查实际覆盖；未指定时使用服务支持的 GFS。"},
        "forecast_interval_hours": {"value": interval, "reason": "ECMWF 当前服务使用 3 小时资料；GFS 优先使用可用的高时间频率资料。"},
        "history_interval_minutes": {"value": request.get("history_interval_minutes", 60), "reason": "默认每小时输出；更密输出会增加存储和后处理成本，不改变边界资料周期。"},
        "assimilation_scheme": {"value": request.get("assimilation_scheme", "off"), "reason": "无明确同化需求时关闭；FDDA 需确保完整时段的驱动场。"},
    }
    if not request.get("start_time") and not request.get("end_time"):
        candidates = []
        for offset in range(3):
            candidate_start = aligned + timedelta(hours=offset * interval)
            candidate_end = candidate_start + timedelta(hours=duration)
            candidate = recommendations({**request, "start_time": iso(candidate_start), "end_time": iso(candidate_end)}, health, pool, now)
            window = candidate["data_window"]
            if window["status"] == "unavailable":
                continue
            candidates.append({"value": {"start_time": iso(candidate_start), "end_time": iso(candidate_end)},
                "data_window": window, "duration_hours": duration,
                "reason": (
                    "按当前服务时间与资料步长对齐；"
                    + ("可复用已下载的 " if window["status"] == "ready" else "名义周期 ")
                    + source.upper() + " " + window["cycle"] + "，"
                    + {"ready": "所需时次已就绪", "needs_download": "所需时次待下载", "waiting_publish": "等待上游发布", "unknown": "资料状态尚未核验"}.get(window["status"], window["status"])
                    + ("；最新周期 " + window["nominal_cycle"]["cycle"] + "：" + {"ready":"已就绪", "needs_download":"待下载", "waiting_publish":"等待发布", "unknown":"未核验"}.get(window["nominal_cycle"]["status"], window["nominal_cycle"]["status"]) if window["cycle"] != window["nominal_cycle"]["cycle"] else "")
                    + "。spin-up另计；实际运行前重新核验资料并按现有策略选周期。")})
        advice["time"]["candidates"] = candidates
    domains = request.get("domains") or []
    cells = sum(d.get("e_we", 0) * d.get("e_sn", 0) for d in domains)
    frames = max(0, int((end-start).total_seconds() / (int(request.get("history_interval_minutes") or 60) * 60)) + 1)
    for key, value in (request.get("physics") or {}).items():
        advice["preset" if key == "preset" else key] = {"value": value, "reason": "来自当前物理预设或已选参数；请结合网格尺度和兼容性提示确认。"}
    from schemas import PBL_SURFACE_COMPATIBILITY
    physics = request.get("physics") or {}
    allowed_surface = PBL_SURFACE_COMPATIBILITY.get(physics.get("bl_pbl_physics"))
    constraints = {}
    if allowed_surface:
        constraints["sf_sfclay_physics"] = {"allowed": sorted(allowed_surface), "reason": "当前服务验证的边界层与近地层兼容组合"}
        if physics.get("sf_sfclay_physics") not in allowed_surface:
            advice["sf_sfclay_physics"] = {"value": min(allowed_surface), "reason": "与当前边界层方案匹配；请确认后应用。"}
    if domains:
        explicit = physics.get("cu_physics_by_domain")
        cu_by_domain = explicit if explicit is not None else [physics["cu_physics"] for _ in domains] if "cu_physics" in physics else [0 if d.get("dx", 0) <= 3000 else 3 for d in domains]
        advice["cu_physics_by_domain"] = {"value": cu_by_domain, "reason": "优先保留明确设置；通用缺省规则为网格距≤3 km关闭积云参数化，其余使用Grell–Freitas (3)。个例适用性仍需验证。"}
    # This relative workload follows each domain's ancestors and has no machine calibration.
    factors = []
    for index, domain in enumerate(domains):
        parent = domain.get("parent_id", index)
        if not index:
            factors.append(1)
        elif type(parent) is int and 1 <= parent <= index and domain.get("dx",0) > 0:
            spacing = domains[parent-1].get("dx",0)
            ratio = spacing // domain["dx"] if spacing % domain["dx"] == 0 else 0
            ancestor = factors[parent-1]
            factors.append(ancestor*ratio if ratio in {3,5} and ancestor is not None else None)
        else:
            factors.append(None)
    weighted_cells = sum(d.get("e_we",0)*d.get("e_sn",0)*factor for d,factor in zip(domains,factors)) if all(f is not None for f in factors) else None
    complete = all(request.get(key) for key in ("start_time", "end_time", "center", "domains"))
    return {"suggestions": advice, "choice_constraints": constraints, "ruleset": RULESET, "rules": configuration_rules(request, complete=complete),
            "data_window": {"source": source, "cycle": cycle, "required_hours": [first, last], "boundary_buffer_hours": forcing_buffer, "nominal_cycle": nominal, "selection_policy": "newest_complete_covering_else_nominal", "missing_hours": missing_hours, "status": coverage, "sampled_at": pool.get("sampled_at") if pool else None},
            "resources": {"status": health.get("status", "unknown"), "sampled_at": health.get("sampled_at"),
                          "runtime_free_gb": health.get("runtime_free_gb"), "grid_cells": cells,
                          "queue_size": health.get("queue_size"), "active_task_count": health.get("active_task_count"),
                          "domain_count":len(domains),"vertical_levels":levels,"three_dimensional_grid_cells":cells*levels,
                          "ancestor_time_step_weighted_cells":weighted_cells*levels if weighted_cells is not None else None,"model_duration_hours":(end-model_start).total_seconds()/3600,
                          "cpu_count": health.get("cpu_count"), "configured_mpi_processes": health.get("configured_mpi_processes"),
                          "memory_available_mb": health.get("memory_available_mb"), "load_average": health.get("load_average"),
                          "output_grid_samples": cells * frames, "runtime_seconds": None, "memory_gb": None,
                          "reason": "格点与输出规模可计算；缺少匹配机器和配置的校准样本，耗时和内存未知。"},
            "alternatives": ["保留原需求等待资料或资源", "缩短模拟时段", "降低网格精度或缩小范围（需重新确认）"],
            "assessed_at": iso(now)}
