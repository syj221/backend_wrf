"""Read-only candidate patches; resolution, never model text, owns validity."""
from copy import deepcopy
from datetime import datetime

from configuration_profile import definition


def candidates(request, health, pool=None):
    from guidance import recommendations
    from namelist_config import resolve
    capability = definition()
    current = deepcopy(request)
    specs = [
        ("data_source", "forcing", "驱动资料", ["data_source", "forecast_interval_hours"],
         [("GFS 全球预报", {"data_source": "gfs", "forecast_interval_hours": current.get("forecast_interval_hours", 1)}),
          ("ECMWF 全球预报", {"data_source": "ecmwf", "forecast_interval_hours": 3})],
         "比较实际资料覆盖；资料可选择不代表所需时次已经就绪。"),
        ("forecast_interval_hours", "forcing", "驱动资料间隔", ["forecast_interval_hours"],
         [(f"{n} 小时边界更新", {"forecast_interval_hours": n}) for n in capability["forecast_intervals"][current.get("data_source", "gfs")]],
         "这是边界资料更新频率，区别于结果输出；较密资料增加读取与存储需求。"),
        ("history_interval_minutes", "output", "结果输出间隔", ["history_interval_minutes"],
         [(f"{n} 分钟输出", {"history_interval_minutes": n}) for n in (60, 30, 180)],
         "更密输出增加存储和后处理成本，不提高边界资料时间分辨率。"),
        ("spinup", "runtime", "spin-up 预运行", ["spinup.hours"],
         [(f"{n} 小时预运行", {"spinup": {"mode": "custom", "hours": n}}) for n in dict.fromkeys((6, 0, current.get("forecast_interval_hours", 1)))],
         "预运行单独计入计算时长；0合法，初始化影响需要个例验证。"),
        ("vertical_levels", "runtime", "垂直层数", ["vertical_levels"],
         [("服务默认 88 层", {"vertical_levels": 88})],
         "当前无经过个例验证的替代层数预设；更多层数不等于更准确。可在完整编辑器自定义并校验模式顶和分层。"),
        ("physics", "physics", "物理方案组合", ["physics"],
         [("保留当前合法物理组合", {})],
         "当前只接入一套通用默认组合，按各域尺度校验积云及关联方案。特定目标可进一步讨论细项，不保证地区最优。"),
        ("assimilation_scheme", "assimilation", "FDDA 网格松弛", ["assimilation_scheme"],
         [("关闭网格松弛", {"assimilation_scheme": "off"})] +
         [(label, {"assimilation_scheme": name}) for label, name in (("弱网格松弛", "fdda_weak"), ("标准网格松弛", "fdda_standard"))
          if (current.get("spinup") or {}).get("hours", 6) > 0 and any(d.get("dx", 0) >= 9000 for d in current.get("domains") or [])],
         "当前受管FDDA只作用于预运行且仅对符合网格尺度条件的域启用；spin-up为0时不能开启。不是WRFDA观测同化。"),
    ]
    result = []
    complete = all(current.get(k) for k in ("center", "domains", "start_time", "end_time"))
    for topic, group, label, paths, values, reason in specs:
        options = []
        for title, patch in values:
            trial = deepcopy(current)
            trial.update(deepcopy(patch))
            status, errors = "requires_configuration", []
            if complete:
                try:
                    resolved = resolve(trial)
                    errors = resolved.get("errors", [])
                    status = "invalid" if errors else "valid"
                except (ValueError, TypeError, KeyError) as exc:
                    status, errors = "invalid", [str(exc)]
            if status == "invalid":
                continue
            coverage = recommendations(trial, health, pool)["data_window"]
            detail = reason
            if topic == "data_source":
                detail += " 覆盖状态：" + coverage["status"] + "。"
            if topic == "history_interval_minutes" and complete:
                duration = (datetime.fromisoformat(str(trial["end_time"]).replace("Z", "+00:00")) - datetime.fromisoformat(str(trial["start_time"]).replace("Z", "+00:00"))).total_seconds()
                detail += f" 正式时段含初始时次约{int(duration // (patch['history_interval_minutes'] * 60)) + 1}个输出时次。"
            options.append({"label": title, "patch": patch, "description": detail,
                            "validation_status": status, "data_window": coverage,
                            "recommended": not options})
        result.append({"id": topic, "field_id": group, "label": label,
                       "target_paths": paths, "reason": reason, "candidates": options})
    return {"schema": "wrf-guidance-1", "items": result}
