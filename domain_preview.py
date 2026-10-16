"""Read-only WPS projected domain preview in the same reference-grid convention."""

from __future__ import annotations

from math import ceil, floor, isfinite
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pyproj import CRS, Transformer, Geod

from schemas import Center, DomainConfig, WrfTaskCreate, validate_domain_tree, LEGACY_CONFIGURATION_PROFILE


class AreaOfInterest(BaseModel):
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def ordered(self):
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("关注区域必须为未跨日期变更线的有效经纬度范围")
        return self


class DomainEdit(BaseModel):
    operation: Literal["draw", "move", "resize"]
    handle: Literal["nw", "ne", "sw", "se", "n", "s", "e", "w"] | None = None
    domain_id: str = Field(pattern=r"^d0[1-4]$")
    bounds: AreaOfInterest | None = None
    start: Center | None = None
    end: Center | None = None

    @model_validator(mode="after")
    def complete(self):
        if self.operation == "draw" and self.bounds is None:
            raise ValueError("重画计算域必须提供框选范围")
        if self.operation == "move" and (self.start is None or self.end is None):
            raise ValueError("拖动计算域必须提供起点和终点")
        if self.operation == "resize" and (self.handle is None or self.end is None):
            raise ValueError("缩放计算域必须提供边角及终点")
        return self


class DomainPreviewRequest(BaseModel):
    center: Center | None = None
    domains: list[DomainConfig] | None = None
    aoi: AreaOfInterest | None = None
    region_half_extent_km: float | None = Field(default=None, gt=0, le=1000)
    resolution_m: int = Field(default=27000, ge=1000, le=50000)
    domain_count: int = Field(default=1, ge=1, le=4)
    nesting_ratio: Literal[3, 5] = 3
    edit: DomainEdit | None = None
    projection: dict[str, Any] = Field(default_factory=dict)
    configuration_profile: Literal['wrf-ordinary-ui-1','wrf-ordinary-ui-2'] | None = None

    @model_validator(mode="after")
    def has_geometry(self):
        if self.center is None and self.aoi is None:
            raise ValueError("请提供已确认中心或关注区域")
        if self.region_half_extent_km is not None and (self.center is None or self.aoi is not None or self.domains is not None or self.edit is not None):
            raise ValueError("建议范围需要中心，且不能覆盖显式关注范围、计算域或地图操作")
        if self.domains is None and self.aoi is None and self.region_half_extent_km is None:
            raise ValueError("请提供嵌套域或关注区域")
        return self


def normalize_projection(center: Center, options=None):
    options = options or {}
    result = {"map_proj": options.get("map_proj", "lambert"),
              "truelat1": options.get("truelat1", center.lat),
              "truelat2": options.get("truelat2", center.lat),
              "stand_lon": options.get("stand_lon", center.lon)}
    if result["map_proj"] not in {"lambert", "mercator", "polar"}:
        raise ValueError("当前米制计算域仅支持 Lambert、Mercator 和 Polar 投影")
    if any(type(result[k]) not in {int, float} or not isfinite(result[k]) for k in ("truelat1", "truelat2", "stand_lon")):
        raise ValueError("投影参数必须是有限数值")
    if not all(-89 < result[k] < 89 for k in ("truelat1", "truelat2")) or not -180 <= result["stand_lon"] <= 180:
        raise ValueError("投影纬度或中央经度越界")
    return result


def _projection(center: Center, options=None) -> CRS:
    options = normalize_projection(center, options)
    name = options.get("map_proj", "lambert")
    lat1, lat2 = options.get("truelat1", center.lat), options.get("truelat2", center.lat)
    lon = options.get("stand_lon", center.lon)
    if name == "lambert" and (abs(lat1 + lat2) < 0.1 or abs(lat1) >= 89 or abs(lat2) >= 89):
        raise ValueError("当前 Lambert 配置不支持赤道或极点中心，请调整中心纬度")
    projection = {"lambert": f"+proj=lcc +lat_1={lat1} +lat_2={lat2} +lat_0={center.lat}",
                  "mercator": f"+proj=merc +lat_ts={lat1}",
                  "polar": f"+proj=stere +lat_0={90 if lat1 >= 0 else -90} +lat_ts={lat1}"}.get(name)
    if projection is None:
        raise ValueError("当前米制计算域仅支持 Lambert、Mercator 和 Polar 投影")
    base = f"{projection} +lon_0={lon} +R=6370000 +units=m +no_defs"
    x, y = Transformer.from_crs("EPSG:4326", CRS.from_proj4(base), always_xy=True).transform(center.lon, center.lat)
    return CRS.from_proj4(base + f" +x_0={-x} +y_0={-y}")


def _area_points(area: AreaOfInterest, transform: Transformer) -> list[tuple[float, float]]:
    # Sample all edges because geographic rectangles curve in WPS coordinates.
    return [transform.transform(lon, lat) for i in range(21) for lon, lat in (
        (area.west + (area.east - area.west) * i / 20, area.south),
        (area.west + (area.east - area.west) * i / 20, area.north),
        (area.west, area.south + (area.north - area.south) * i / 20),
        (area.east, area.south + (area.north - area.south) * i / 20),
    )]


def _origins(center: Center, domains: list[DomainConfig]) -> list[tuple[float, float]]:
    outer = domains[0]
    result = [(-((center.grid_x or (outer.e_we + 1) / 2) - 1) * outer.dx,
               -((center.grid_y or (outer.e_sn + 1) / 2) - 1) * outer.dy)]
    for i, domain in enumerate(domains[1:], 1):
        parent_index = domain.parent_id - 1
        parent = domains[parent_index]
        result.append((result[parent_index][0] + (domain.i_parent_start - 1) * parent.dx,
                       result[parent_index][1] + (domain.j_parent_start - 1) * parent.dy))
    return result


def _apply_edit(payload: DomainPreviewRequest) -> tuple[Center, list[DomainConfig], list[dict]]:
    if payload.center is None or not payload.domains:
        raise ValueError("编辑计算域需要原始中心与嵌套域")
    center = payload.center.model_copy(deep=True)
    domains = [domain.model_copy(deep=True) for domain in payload.domains]
    validate_domain_tree(domains, legacy=payload.configuration_profile == LEGACY_CONFIGURATION_PROFILE)
    edit = payload.edit
    assert edit is not None
    index = next((i for i, domain in enumerate(domains) if domain.id == edit.domain_id), -1)
    if index < 0:
        raise ValueError("所选计算域不存在")
    originals = [d.model_dump() for d in domains]
    original = domains[index].model_dump()
    original_center = center.model_dump()
    projected = Transformer.from_crs("EPSG:4326", _projection(center, payload.projection), always_xy=True)
    origins = _origins(center, domains)
    domain = domains[index]
    if edit.operation == "move":
        assert edit.start and edit.end
        x0, y0 = projected.transform(edit.start.lon, edit.start.lat)
        x1, y1 = projected.transform(edit.end.lon, edit.end.lat)
        if index == 0:
            inverse = Transformer.from_crs(_projection(center, payload.projection), "EPSG:4326", always_xy=True)
            lon, lat = inverse.transform(x1 - x0, y1 - y0)
            center = Center(lat=lat, lon=lon, grid_x=center.grid_x, grid_y=center.grid_y)
        else:
            parent = domains[domain.parent_id - 1]
            domain.i_parent_start += round((x1 - x0) / parent.dx)
            domain.j_parent_start += round((y1 - y0) / parent.dy)
    elif edit.operation == "resize":
        assert edit.end and edit.handle
        x, y = projected.transform(edit.end.lon, edit.end.lat)
        west, south = origins[index]
        east, north = west + (domain.e_we-1)*domain.dx, south + (domain.e_sn-1)*domain.dy
        parent_index = domain.parent_id - 1
        step_x = domains[parent_index].dx if index else domain.dx
        step_y = domains[parent_index].dy if index else domain.dy
        anchor_x, anchor_y = origins[parent_index] if index else origins[0]
        snap_x = anchor_x + round((x-anchor_x)/step_x)*step_x
        snap_y = anchor_y + round((y-anchor_y)/step_y)*step_y
        if "w" in edit.handle: west = snap_x
        if "e" in edit.handle: east = snap_x
        if "s" in edit.handle: south = snap_y
        if "n" in edit.handle: north = snap_y
        if east <= west or north <= south:
            raise ValueError("不能将边角拖过相对边界")
        domain.e_we = round((east-west)/domain.dx)+1
        domain.e_sn = round((north-south)/domain.dy)+1
        if index:
            domain.i_parent_start = round((west-anchor_x)/step_x)+1
            domain.j_parent_start = round((south-anchor_y)/step_y)+1
        else:
            center.grid_x, center.grid_y = 1-west/domain.dx, 1-south/domain.dy
            if not (1 <= center.grid_x <= domain.e_we and 1 <= center.grid_y <= domain.e_sn):
                raise ValueError("缩放后参考点必须仍位于 D01 内")
        for child_index, child in enumerate(domains):
            if child.parent_id != index+1:
                continue
            child.i_parent_start = round((origins[child_index][0]-west)/domain.dx)+1
            child.j_parent_start = round((origins[child_index][1]-south)/domain.dy)+1
            if child.i_parent_start < 1 or child.j_parent_start < 1:
                raise ValueError("缩放会越过子域；子域位置和分辨率不能被静默修改")
    else:
        assert edit.bounds
        if index == 0:
            center = Center(lat=(edit.bounds.south + edit.bounds.north) / 2,
                            lon=(edit.bounds.west + edit.bounds.east) / 2)
            projected = Transformer.from_crs("EPSG:4326", _projection(center, payload.projection), always_xy=True)
        points = _area_points(edit.bounds, projected)
        west, east = min(x for x, _ in points), max(x for x, _ in points)
        south, north = min(y for _, y in points), max(y for _, y in points)
        if index == 0:
            domain.e_we = max(10, ceil(2 * max(abs(west), abs(east)) / domain.dx) + 1)
            domain.e_sn = max(10, ceil(2 * max(abs(south), abs(north)) / domain.dy) + 1)
        else:
            parent_index = domain.parent_id - 1
            parent = domains[parent_index]
            ratio = parent.dx / domain.dx
            if ratio not in {3, 5} or parent.dy / domain.dy != ratio:
                raise ValueError("子域嵌套比必须为 3 或 5")
            ratio = int(ratio)
            parent_x, parent_y = origins[parent_index]
            domain.i_parent_start = floor((west - parent_x) / parent.dx) + 1
            domain.j_parent_start = floor((south - parent_y) / parent.dy) + 1
            x = parent_x + (domain.i_parent_start - 1) * parent.dx
            y = parent_y + (domain.j_parent_start - 1) * parent.dy
            domain.e_we = max(ceil(9 / ratio), ceil((east - x) / parent.dx)) * ratio + 1
            domain.e_sn = max(ceil(9 / ratio), ceil((north - y) / parent.dy)) * ratio + 1
    changes = [{"field": f"{d.id}.{key}", "before": before[key], "after": value}
               for d,before in zip(domains, originals) for key,value in d.model_dump().items() if before[key] != value]
    changes += [{"field": f"center.{key}", "before": original_center[key], "after": value}
                for key, value in center.model_dump().items() if original_center[key] != value]
    return center, domains, changes


def _outline(west: float, south: float, east: float, north: float, to_lonlat: Transformer) -> list[list[float]]:
    # Densify each projected edge: straight WPS grid edges curve on a lon/lat map.
    points: list[list[float]] = []
    edges = (
        ((west, south), (east, south)),
        ((east, south), (east, north)),
        ((east, north), (west, north)),
        ((west, north), (west, south)),
    )
    for (x0, y0), (x1, y1) in edges:
        for index in range(20):
            ratio = index / 20
            x, y = x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio
            lon, lat = to_lonlat.transform(x, y)
            points.append([round(lon, 7), round(lat, 7)])
    points.append(points[0])
    return points


def preview_domains(payload: DomainPreviewRequest) -> dict[str, Any]:
    changes = []
    if payload.region_half_extent_km is not None:
        geod = Geod(ellps="WGS84")
        c, distance = payload.center, payload.region_half_extent_km * 1000
        west = geod.fwd(c.lon, c.lat, 270, distance)[0]
        east = geod.fwd(c.lon, c.lat, 90, distance)[0]
        south = geod.fwd(c.lon, c.lat, 180, distance)[1]
        north = geod.fwd(c.lon, c.lat, 0, distance)[1]
        payload = payload.model_copy(update={"aoi": AreaOfInterest(west=west, east=east, south=south, north=north)})
    if payload.center:
        payload = payload.model_copy(update={"projection":normalize_projection(payload.center, payload.projection)})
    if payload.edit:
        center, domains, changes = _apply_edit(payload)
        payload = payload.model_copy(update={"center": center, "domains": domains})
    center = payload.center
    if center is None:
        assert payload.aoi is not None
        center = Center(lat=(payload.aoi.south + payload.aoi.north) / 2,
                        lon=(payload.aoi.west + payload.aoi.east) / 2)
    payload = payload.model_copy(update={"projection": normalize_projection(center, payload.projection)})
    crs = _projection(center, payload.projection)
    to_projected = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_lonlat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    domains = payload.domains
    if domains is None:
        assert payload.aoi is not None
        corners = _area_points(payload.aoi, to_projected)
        xs, ys = [point[0] for point in corners], [point[1] for point in corners]
        dx = payload.resolution_m
        e_we = ceil(2 * max(abs(x) for x in xs) / dx) + 3
        e_sn = ceil(2 * max(abs(y) for y in ys) / dx) + 3
        if max(e_we, e_sn) > 500:
            raise ValueError("所选关注区域在该分辨率下超过 500 格点上限；请缩小范围或选择较粗网格")
        domains = [DomainConfig(id="d01", dx=dx, dy=dx, e_we=max(10, e_we),
                                e_sn=max(10, e_sn), parent_id=0)]
        if payload.domain_count > 1:
            ratio = payload.nesting_ratio
            if dx * ratio ** (payload.domain_count - 1) > 50000:
                raise ValueError("所选层数和嵌套比使外层超过 50 km；请比较更细目标网格或更少层数，不自动替换需求")
            # Centered odd grids keep all nest starts on parent-grid points.
            def segments(required, divisor):
                return ceil(max(100, required) / (2 * divisor)) * 2 * divisor
            nx = segments(2 * max(abs(x) for x in xs) / dx + 4, ratio)
            ny = segments(2 * max(abs(y) for y in ys) / dx + 4, ratio)
            dimensions = [(nx, ny)]
            for _ in range(payload.domain_count - 1):
                nx = segments(nx / ratio + 20, ratio)
                ny = segments(ny / ratio + 20, ratio)
                dimensions.append((nx, ny))
            domains = []
            for index, (nx, ny) in enumerate(reversed(dimensions)):
                if max(nx, ny) >= 500:
                    raise ValueError("嵌套候选超过 500 格点上限；请缩小关注区域或比较较粗网格")
                spacing = dx * ratio ** (payload.domain_count - index - 1)
                parent = domains[index - 1] if index else None
                domains.append(DomainConfig(id=f"d{index + 1:02d}", dx=spacing, dy=spacing,
                    e_we=nx + 1, e_sn=ny + 1, parent_id=index,
                    parent_grid_ratio=ratio if index else 1,
                    i_parent_start=int(((parent.e_we - 1) - nx / ratio) / 2) + 1 if parent else 1,
                    j_parent_start=int(((parent.e_sn - 1) - ny / ratio) / 2) + 1 if parent else 1))
    # Reuse the authoritative normal-WRF nesting and coverage validator.
    verified = WrfTaskCreate.model_validate({
        "data_source": "gfs", "start_time": "2026-01-01T06:00:00Z",
        "end_time": "2026-01-01T12:00:00Z",
        "center": center.model_dump(), "domains": [item.model_dump() for item in domains],
        "configuration_profile":payload.configuration_profile,
    })
    domains = verified.domains
    if payload.configuration_profile:
        from configuration_profile import validate_geometry
        validate_geometry([d.model_dump() for d in domains])
    result = []
    origins = _origins(center, domains)
    for index, domain in enumerate(domains):
        x, y = origins[index]
        west, south = x - domain.dx / 2, y - domain.dy / 2
        east = x + (domain.e_we - 0.5) * domain.dx
        north = y + (domain.e_sn - 0.5) * domain.dy
        ring = _outline(west, south, east, north, to_lonlat)
        result.append({
            "id": domain.id, "dx": domain.dx, "dy": domain.dy,
            "grid": [domain.e_we, domain.e_sn], "parent_id":domain.parent_id,"parent_grid_ratio": domain.parent_grid_ratio,
            "polygon": {"type": "Polygon", "coordinates": [ring]},
            "bbox": [min(point[0] for point in ring), min(point[1] for point in ring),
                     max(point[0] for point in ring), max(point[1] for point in ring)],
            "projected_bounds_m": [west, south, east, north],
            "corners": {name: [round(lon,7),round(lat,7)] for name,(lon,lat) in zip(
                ("sw","se","ne","nw"), [to_lonlat.transform(px,py) for px,py in (
                    (x,y),(x+(domain.e_we-1)*domain.dx,y),
                    (x+(domain.e_we-1)*domain.dx,y+(domain.e_sn-1)*domain.dy),
                    (x,y+(domain.e_sn-1)*domain.dy))])},
        })
    aoi_covered = None
    finest_aoi_covered = None
    if payload.aoi is not None:
        box = result[0]["projected_bounds_m"]
        aoi_covered = all(box[0] <= x <= box[2] and box[1] <= y <= box[3]
                          for x, y in _area_points(payload.aoi, to_projected))
        finest = min(d.dx for d in domains)
        points = _area_points(payload.aoi, to_projected)
        finest_aoi_covered = any(all(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x,y in points)
            for d, item in zip(domains,result) if d.dx == finest for box in [item["projected_bounds_m"]])
    warnings = []
    if any(d.e_we < 100 or d.e_sn < 100 for d in domains):
        warnings.append("官方经验建议计算域不少于约100×100格点；这不是当前服务的硬性下限。")
    if any(min(d.i_parent_start-1,d.j_parent_start-1,
               domains[d.parent_id-1].e_we-d.i_parent_start-(d.e_we-1)/d.parent_grid_ratio,
               domains[d.parent_id-1].e_sn-d.j_parent_start-(d.e_sn-1)/d.parent_grid_ratio)<5 for i,d in enumerate(domains) if i):
        warnings.append("官方经验建议子域与父域边界至少相距5个父域格点；当前配置仍须通过包含及对齐硬约束。")
    return {
        "center": center.model_dump(), "center_confirmed": payload.center is not None,
        "domains": [item.model_dump() for item in domains], "projection": payload.projection.get("map_proj", "lambert"),
        "projection_parameters": payload.projection,
        "earth_radius_m": 6370000, "boundaries": result,
        "default_domain":"d01",
        "aoi": payload.aoi.model_dump() if payload.aoi else None,
        "aoi_covered": aoi_covered,
        "finest_aoi_covered": finest_aoi_covered,
        "adjustments": changes,
        "requested_bounds": payload.edit.bounds.model_dump() if payload.edit and payload.edit.bounds else None,
        "warnings": warnings,
    }
