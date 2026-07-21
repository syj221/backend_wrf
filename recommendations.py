from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Settings
from hpc import HpcClient


RECOMMENDATION_VERSION = "2026.07-geography-season-v2"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class RecommendationManager:
    def __init__(self, settings: Settings, hpc: HpcClient):
        self.settings = settings
        self.hpc = hpc
        self.cache_dir = settings.data_dir / "recommendations"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wrf-recommend")

    def stop(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=False)

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        geometry = {"center": request["center"], "domains": request["domains"]}
        geography_fingerprint = _digest(geometry)
        recommendation_fingerprint = _digest({"version": RECOMMENDATION_VERSION, "request": request})
        job_id = "rec_" + uuid.uuid4().hex[:16]
        cache_path = self.cache_dir / f"{recommendation_fingerprint}.json"
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        job = {
            "id": job_id,
            "status": "succeeded" if isinstance(cached, dict) else "queued",
            "fingerprint": recommendation_fingerprint,
            "geography_fingerprint": geography_fingerprint,
            "created_at": _utc_now(),
            "result": cached,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = job
        if cached is None:
            self._pool.submit(self._run, job_id, request, cache_path)
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return json.loads(json.dumps(job, ensure_ascii=False)) if job else None

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values, updated_at=_utc_now())

    def _run(self, job_id: str, request: dict[str, Any], cache_path: Path) -> None:
        self._update(job_id, status="analyzing_geography")
        try:
            job = self.get(job_id) or {}
            geography = self.hpc.analyze_geography(job["geography_fingerprint"], request)
            result = self._recommend(request, geography)
            temporary = cache_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, cache_path)
            self._update(job_id, status="succeeded", result=result, error=None)
        except Exception as exc:
            self._update(job_id, status="failed", error=str(exc))

    @staticmethod
    def _recommend(request: dict[str, Any], geography: dict[str, Any]) -> dict[str, Any]:
        focus = request.get("forecast_focus", "general")
        domains = request["domains"]
        geo_by_domain = {item["domain"]: item for item in geography.get("domains") or []}
        signed_latitude = float(request["center"]["lat"])
        latitude = abs(signed_latitude)
        month = datetime.fromisoformat(str(request["start_time"]).replace("Z", "+00:00")).month
        tropical = latitude < 23.5
        if tropical:
            season = "热带全年对流活跃期"
        else:
            warm_months = {5, 6, 7, 8, 9} if signed_latitude >= 0 else {11, 12, 1, 2, 3}
            season = "暖季" if month in warm_months else "冷季"
        cold_season = season == "冷季"
        geo_domains = list(geography.get("domains") or [])
        terrain_trigger = any(
            float(item.get("terrain_std_m", 0)) >= 300 or float(item.get("terrain_range_m", 0)) >= 1000
            for item in geo_domains
        )
        max_terrain = max((float(item.get("terrain_max_m", 0)) for item in geo_domains), default=0)
        max_terrain_range = max((float(item.get("terrain_range_m", 0)) for item in geo_domains), default=0)
        max_terrain_std = max((float(item.get("terrain_std_m", 0)) for item in geo_domains), default=0)
        max_water = max((float(item.get("water_fraction", 0)) for item in geo_domains), default=0)
        max_urban = max((float(item.get("urban_fraction", 0)) for item in geo_domains), default=0)
        coastal_trigger = any(
            0.15 <= float(item.get("water_fraction", 0)) <= 0.85 for item in geo_domains
        )
        if focus == "snowfall" or (cold_season and latitude >= 35):
            preset = "冷季降雪 / 混合相态"
            mp_physics = 16
        elif focus == "convection" and (season != "冷季" or tropical):
            preset = "暖季强对流 / 降水"
            mp_physics = 6
        else:
            preset = "城市精细预报" if focus == "urban" else ("温度 / 风场" if focus == "temperature_wind" else "地理季节综合推荐")
            mp_physics = 8
        if focus == "temperature_wind":
            bl_pbl_physics, sf_sfclay_physics = 5, 5
        elif terrain_trigger or focus == "snowfall":
            bl_pbl_physics, sf_sfclay_physics = 2, 2
        else:
            bl_pbl_physics, sf_sfclay_physics = 1, 1
        cu: list[int] = []
        urban: list[int] = []
        for domain in domains:
            dx = int(domain["dx"])
            if dx >= 10000:
                cu.append(16 if focus == "convection" or tropical else 1)
            elif dx > 3000:
                cu.append(3)
            else:
                cu.append(0)
            geo = geo_by_domain.get(domain["id"], {})
            urban.append(1 if focus == "urban" and dx <= 3000 and float(geo.get("urban_fraction", 0)) >= 0.15 else 0)
        fine_trigger = min(int(item["dx"]) for item in domains) <= 3000
        spinup_hours = 12 if focus in {"convection", "urban", "snowfall"} or terrain_trigger or fine_trigger else 6
        radt = max(1, min(15, round(min(int(item["dx"]) for item in domains) / 1000)))
        physics = {
            "preset": preset,
            "mp_physics": mp_physics,
            "cu_physics": cu[0],
            "cu_physics_by_domain": cu,
            "ra_lw_physics": 4,
            "ra_sw_physics": 4,
            "bl_pbl_physics": bl_pbl_physics,
            "sf_sfclay_physics": sf_sfclay_physics,
            "sf_surface_physics": 2,
            "sf_urban_physics": urban[0],
            "sf_urban_physics_by_domain": urban,
            "num_soil_layers": 4,
            "num_land_cat": 21,
            "radt": radt,
        }
        reasons = [
            f"中心纬度 {signed_latitude:.2f}°、起报月份 {month} 月，判定为{season}；据此选择微物理 {mp_physics}。",
            f"geogrid 地形最高约 {max_terrain:.0f} m、起伏 {max_terrain_range:.0f} m、标准差 {max_terrain_std:.0f} m；边界层/近地层采用 {bl_pbl_physics}/{sf_sfclay_physics}。",
            f"区域最大水体比例 {max_water:.0%}、城市比例 {max_urban:.0%}；城市冠层按逐域城市比例和网格距设置。",
            f"积云参数按网格距逐域设置为 {cu}：≥10 km 参数化、3–10 km 尺度感知、≤3 km 显式对流。",
            f"综合关注点、最细网格、地形和下垫面，建议 {spinup_hours} 小时 spin-up，辐射调用间隔 {radt} 分钟。",
            "网格松弛只建议用于 spin-up 的 ≥9 km 粗网格，且不松弛水汽和边界层。",
        ]
        if terrain_trigger:
            reasons.append("geogrid 显示复杂地形，延长 spin-up 以减小初始调整噪声。")
        if coastal_trigger:
            reasons.append("嵌套域同时包含显著陆地和水体，方案保留 Noah 陆面过程并强调近地层陆海差异。")
        if focus == "urban" and not any(urban):
            reasons.append("当前最内层城市比例不足 15%，未自动开启 UCM。")
        warnings = ["该方案是规则化起点，不代表对个例最优；正式业务应通过历史个例回报检验。"]
        if focus == "snowfall" and not cold_season and not tropical:
            warnings.append("所选月份处于当地暖季，请再次确认降雪预报关注点。")
        if tropical and focus == "snowfall":
            warnings.append("中心位于热带纬度，降雪方案仅适用于明确的高海拔个例。")
        assimilation_scheme = "fdda_standard" if int(domains[0]["dx"]) >= 9000 and spinup_hours else "off"
        return {
            "fingerprint": _digest({"version": RECOMMENDATION_VERSION, "request": request}),
            "version": RECOMMENDATION_VERSION,
            "generated_at": _utc_now(),
            "forecast_focus": focus,
            "physics": physics,
            "spinup": {"mode": "custom", "hours": spinup_hours},
            "assimilation_scheme": assimilation_scheme,
            "geography": geography,
            "factors": {
                "latitude": signed_latitude,
                "month": month,
                "season": season,
                "complex_terrain": terrain_trigger,
                "coastal": coastal_trigger,
                "max_water_fraction": max_water,
                "max_urban_fraction": max_urban,
                "finest_dx_m": min(int(item["dx"]) for item in domains),
            },
            "confidence": 0.9 if len(geo_domains) == len(domains) else 0.78,
            "reasons": reasons,
            "warnings": warnings,
        }
