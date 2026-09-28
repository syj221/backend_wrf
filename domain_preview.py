"""Read-only WPS Lambert domain preview in the same reference-grid convention."""

from __future__ import annotations

from math import ceil, floor
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from pyproj import CRS, Transformer

from schemas import Center, DomainConfig, WrfTaskCreate


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
    operation: Literal["draw", "move"]
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
        return self


class DomainPreviewRequest(BaseModel):
    center: Center | None = None
    domains: list[DomainConfig] | None = None
    aoi: AreaOfInterest | None = None
    resolution_m: int = Field(default=27000, ge=1000, le=50000)
    edit: DomainEdit | None = None

    @model_validator(mode="after")
    def has_geometry(self):
        if self.center is None and self.aoi is None:
            raise ValueError("请提供已确认中心或关注区域")
        if self.domains is None and self.aoi is None:
            raise ValueError("请提供嵌套域或关注区域")
        return self


def _projection(center: Center) -> CRS:
    if abs(center.lat) < 0.1 or abs(center.lat) >= 89:
        raise ValueError("当前 Lambert 配置不支持赤道或极点中心，请调整中心纬度")
    return CRS.from_proj4(
        f"+proj=lcc +lat_1={center.lat} +lat_2={center.lat} "
        f"+lat_0={center.lat} +lon_0={center.lon} +R=6370000 +units=m +no_defs"
    )


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
        parent = domains[i - 1]
        result.append((result[-1][0] + (domain.i_parent_start - 1) * parent.dx,
                       result[-1][1] + (domain.j_parent_start - 1) * parent.dy))
    return result


def _apply_edit(payload: DomainPreviewRequest) -> tuple[Center, list[DomainConfig], list[dict]]:
    if payload.center is None or not payload.domains:
        raise ValueError("编辑计算域需要原始中心与嵌套域")
    center = payload.center.model_copy(deep=True)
    domains = [domain.model_copy(deep=True) for domain in payload.domains]
    edit = payload.edit
    assert edit is not None
    index = next((i for i, domain in enumerate(domains) if domain.id == edit.domain_id), -1)
    if index < 0:
        raise ValueError("所选计算域不存在")
    original = domains[index].model_dump()
    original_center = center.model_dump()
    projected = Transformer.from_crs("EPSG:4326", _projection(center), always_xy=True)
    origins = _origins(center, domains)
    domain = domains[index]
    if edit.operation == "move":
        assert edit.start and edit.end
        x0, y0 = projected.transform(edit.start.lon, edit.start.lat)
        x1, y1 = projected.transform(edit.end.lon, edit.end.lat)
        if index == 0:
            inverse = Transformer.from_crs(_projection(center), "EPSG:4326", always_xy=True)
            lon, lat = inverse.transform(x1 - x0, y1 - y0)
            center = Center(lat=lat, lon=lon, grid_x=center.grid_x, grid_y=center.grid_y)
        else:
            parent = domains[index - 1]
            domain.i_parent_start += round((x1 - x0) / parent.dx)
            domain.j_parent_start += round((y1 - y0) / parent.dy)
    else:
        assert edit.bounds
        if index == 0:
            center = Center(lat=(edit.bounds.south + edit.bounds.north) / 2,
                            lon=(edit.bounds.west + edit.bounds.east) / 2)
            projected = Transformer.from_crs("EPSG:4326", _projection(center), always_xy=True)
        points = _area_points(edit.bounds, projected)
        west, east = min(x for x, _ in points), max(x for x, _ in points)
        south, north = min(y for _, y in points), max(y for _, y in points)
        if index == 0:
            domain.e_we = max(10, ceil(2 * max(abs(west), abs(east)) / domain.dx) + 1)
            domain.e_sn = max(10, ceil(2 * max(abs(south), abs(north)) / domain.dy) + 1)
        else:
            parent = domains[index - 1]
            ratio = parent.dx / domain.dx
            if ratio not in {3, 5} or parent.dy / domain.dy != ratio:
                raise ValueError("子域嵌套比必须为 3 或 5")
            ratio = int(ratio)
            parent_x, parent_y = origins[index - 1]
            domain.i_parent_start = floor((west - parent_x) / parent.dx) + 1
            domain.j_parent_start = floor((south - parent_y) / parent.dy) + 1
            x = parent_x + (domain.i_parent_start - 1) * parent.dx
            y = parent_y + (domain.j_parent_start - 1) * parent.dy
            domain.e_we = max(ceil(9 / ratio), ceil((east - x) / parent.dx)) * ratio + 1
            domain.e_sn = max(ceil(9 / ratio), ceil((north - y) / parent.dy)) * ratio + 1
    changes = [{"field": f"{domain.id}.{key}", "before": original[key], "after": value}
               for key, value in domain.model_dump().items() if original[key] != value]
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
    if payload.edit:
        center, domains, changes = _apply_edit(payload)
        payload = payload.model_copy(update={"center": center, "domains": domains})
    center = payload.center
    if center is None:
        assert payload.aoi is not None
        center = Center(lat=(payload.aoi.south + payload.aoi.north) / 2,
                        lon=(payload.aoi.west + payload.aoi.east) / 2)
    crs = _projection(center)
    to_projected = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_lonlat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    domains = payload.domains
    if domains is None:
        assert payload.aoi is not None
        corners = _area_points(payload.aoi, to_projected)
        xs, ys = [point[0] for point in corners], [point[1] for point in corners]
        dx = payload.resolution_m
        e_we = ceil((max(xs) - min(xs)) / dx) + 3
        e_sn = ceil((max(ys) - min(ys)) / dx) + 3
        if max(e_we, e_sn) > 500:
            raise ValueError("所选关注区域在该分辨率下超过 500 格点上限；请缩小范围或选择较粗网格")
        domains = [DomainConfig(id="d01", dx=dx, dy=dx, e_we=max(10, e_we),
                                e_sn=max(10, e_sn), parent_id=0)]
    # Reuse the authoritative normal-WRF nesting and coverage validator.
    verified = WrfTaskCreate.model_validate({
        "data_source": "gfs", "start_time": "2026-01-01T06:00:00Z",
        "end_time": "2026-01-01T12:00:00Z",
        "center": center.model_dump(), "domains": [item.model_dump() for item in domains],
    })
    domains = verified.domains
    outer = domains[0]
    origin_x = -((center.grid_x or (outer.e_we + 1) / 2) - 1) * outer.dx
    origin_y = -((center.grid_y or (outer.e_sn + 1) / 2) - 1) * outer.dy
    result = []
    origins = [(origin_x, origin_y)]
    for index, domain in enumerate(domains):
        if index:
            parent = domains[index - 1]
            parent_x, parent_y = origins[index - 1]
            origins.append((parent_x + (domain.i_parent_start - 1) * parent.dx,
                            parent_y + (domain.j_parent_start - 1) * parent.dy))
        x, y = origins[index]
        west, south = x - domain.dx / 2, y - domain.dy / 2
        east = x + (domain.e_we - 0.5) * domain.dx
        north = y + (domain.e_sn - 0.5) * domain.dy
        ring = _outline(west, south, east, north, to_lonlat)
        result.append({
            "id": domain.id, "dx": domain.dx, "dy": domain.dy,
            "grid": [domain.e_we, domain.e_sn], "parent_grid_ratio": domain.parent_grid_ratio,
            "polygon": {"type": "Polygon", "coordinates": [ring]},
            "bbox": [min(point[0] for point in ring), min(point[1] for point in ring),
                     max(point[0] for point in ring), max(point[1] for point in ring)],
            "projected_bounds_m": [west, south, east, north],
        })
    aoi_covered = None
    if payload.aoi is not None:
        box = result[0]["projected_bounds_m"]
        aoi_covered = all(box[0] <= x <= box[2] and box[1] <= y <= box[3]
                          for x, y in _area_points(payload.aoi, to_projected))
    return {
        "center": center.model_dump(), "center_confirmed": payload.center is not None,
        "domains": [item.model_dump() for item in domains], "projection": "WPS Lambert conformal",
        "earth_radius_m": 6370000, "boundaries": result,
        "aoi": payload.aoi.model_dump() if payload.aoi else None,
        "aoi_covered": aoi_covered,
        "adjustments": changes,
        "requested_bounds": payload.edit.bounds.model_dump() if payload.edit and payload.edit.bounds else None,
        "warnings": [] if aoi_covered is not False else ["关注区域超出 D01；当前域不能直接提交。"],
    }
