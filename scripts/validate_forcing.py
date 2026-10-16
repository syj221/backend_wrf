#!/usr/bin/env python3
"""Decode actual GRIB metadata; hashes and GRIB sentinels alone are insufficient."""
import argparse
import datetime as dt
import json
from pathlib import Path

SOIL_TEMPERATURE = {'st', 'stl1', 'sot'}
SOIL_MOISTURE = {'swvl1', 'soilw', 'swvl', 'vsw'}


def soil_layer(get, level_type):
    """Preserve fractional depths and distinguish WMO numeric soil layers."""
    if level_type not in ('depthBelowLandLayer', 'depthBelowLand', 'soilLayer', 'soilLevel'):
        return None
    try:
        first = float(get('scaledValueOfFirstFixedSurface')) * 10 ** -int(get('scaleFactorOfFirstFixedSurface'))
        second = float(get('scaledValueOfSecondFixedSurface')) * 10 ** -int(get('scaleFactorOfSecondFixedSurface'))
    except (ValueError, TypeError, OverflowError):
        return None
    if not 0 <= first < second < 10000:
        return None
    return ('index' if level_type.startswith('soil') else 'metres', first, second)


def validate(path, valid_time=None, bounds=None, p_top_pa=5000):
    from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release
    fields, pressure, soils, times, boxes = set(), set(), set(), set(), []
    pressure_fields, soil_fields, grids = {}, {}, set()
    count = 0
    with path.open('rb') as stream:
        while True:
            handle = codes_grib_new_from_file(stream)
            if handle is None: break
            try:
                count += 1
                name = str(codes_get(handle, 'shortName'))
                level_type = str(codes_get(handle, 'typeOfLevel'))
                level = float(codes_get(handle, 'level'))
                fields.add(name)
                if level_type in ('isobaricInhPa', 'isobaricInPa'):
                    pressure_level = level / 100 if level_type == 'isobaricInPa' else level
                    pressure.add(pressure_level)
                    pressure_fields.setdefault(name, set()).add(pressure_level)
                if name in SOIL_TEMPERATURE | SOIL_MOISTURE:
                    layer = soil_layer(lambda key: codes_get(handle, key), level_type)
                    if layer:
                        soils.add(layer)
                        soil_fields.setdefault(name, set()).add(layer)
                times.add('%08d%04d' % (codes_get(handle, 'validityDate'), codes_get(handle, 'validityTime')))
                if name in ('t', 'u', 'v', 'r', 'q', 'gh', 'z') and level_type.startswith('isobaric'):
                    grid = codes_get(handle, 'md5GridSection')
                    if grid in grids: continue
                    grids.add(grid)
                    lats = codes_get_array(handle, 'latitudes'); lons = codes_get_array(handle, 'longitudes')
                    # WRF service supports China-centred domains; normalize the
                    # GRIB convention before checking actual geographic extent.
                    lons = ((lons + 180) % 360) - 180
                    boxes.append((float(lats.min()),float(lons.min()),float(lats.max()),float(lons.max())))
            finally:
                codes_release(handle)
    issues = []
    for label, alternatives in (('temperature', {'t'}),('u_wind',{'u'}),('v_wind',{'v'}),
                                 ('humidity',{'r','q'}),('geopotential',{'gh','z'}),
                                 ('surface_pressure',{'sp','pres'}),('surface_temperature',{'2t','t2m','t'}),
                                 ('soil_temperature',{'st','stl1','sot'}),('soil_moisture',{'swvl1','soilw','swvl','vsw'})):
        if not fields.intersection(alternatives): issues.append('missing_' + label)
    common = set(pressure)
    for names in (('t',),('u',),('v',),('r','q'),('gh','z')):
        levels=set()
        for name in names: levels.update(pressure_fields.get(name,set()))
        common.intersection_update(levels)
    if len(common) < 10: issues.append('insufficient_common_pressure_levels')
    if not common or min(common) * 100 > p_top_pa:
        issues.append('pressure_levels_below_model_top')
    temperature_layers = set().union(*(soil_fields.get(name,set()) for name in SOIL_TEMPERATURE))
    moisture_layers = set().union(*(soil_fields.get(name,set()) for name in SOIL_MOISTURE))
    if len(temperature_layers & moisture_layers) < 4: issues.append('insufficient_common_soil_layers')
    if valid_time and valid_time not in times: issues.append('missing_valid_time')
    if bounds and (not boxes or any(b[0] > bounds[0] or b[1] > bounds[1] or b[2] < bounds[2] or b[3] < bounds[3] for b in boxes)):
        issues.append('insufficient_spatial_coverage')
    return {'schema':'wrf-forcing-validation-1','name':path.name,'messages':count,
            'fields':sorted(fields),'pressure_levels':sorted(pressure),'soil_layers':sorted(soils),
            'valid_times':sorted(times),'issues':issues,'valid':count>0 and not issues}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path); parser.add_argument('--valid-time')
    parser.add_argument('--bounds', nargs=4, type=float)
    parser.add_argument('--p-top-pa', type=float, default=5000)
    args = parser.parse_args()
    print(json.dumps(validate(args.path, args.valid_time, args.bounds, args.p_top_pa)))
