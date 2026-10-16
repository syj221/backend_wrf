#!/usr/bin/env python3
"""Validate every domain/time and numeric field before rendering or publication."""
import argparse
import datetime as dt
import json
from pathlib import Path

import netCDF4
import numpy as np
import f90nml
try:
    from .output_schedule import history_settings
except ImportError:
    from output_schedule import history_settings

REQUIRED = ('Times', 'XLAT', 'XLONG', 'T2', 'U10', 'V10', 'PSFC', 'SWDOWN', 'RAINC', 'RAINNC')


def validate_geometry(data, domain, config, projection):
    """Bind native geometry to the frozen configuration, not just valid units."""
    for attr, key in (('PARENT_ID', 'parent_id'), ('I_PARENT_START', 'i_parent_start'),
                      ('J_PARENT_START', 'j_parent_start')):
        if key in domain and int(getattr(data, attr)) != int(domain[key]):
            raise ValueError('domain_topology_mismatch:' + attr)
    levels = config.get('vertical_levels')
    if levels is not None and len(data.dimensions['bottom_top']) != int(levels) - 1:
        raise ValueError('vertical_grid_mismatch')
    if not projection:
        return
    expected_projection = {'lambert': 1, 'polar': 2, 'mercator': 3}.get(projection.get('map_proj'))
    if expected_projection is None or int(data.MAP_PROJ) != expected_projection:
        raise ValueError('projection_mismatch:MAP_PROJ')
    for attr, key in (('TRUELAT1', 'truelat1'), ('TRUELAT2', 'truelat2'), ('STAND_LON', 'stand_lon')):
        if key not in projection or (attr == 'TRUELAT2' and expected_projection != 1):
            continue
        if not np.isclose(float(getattr(data, attr)), float(projection[key]), rtol=0, atol=1e-4):
            raise ValueError('projection_mismatch:' + attr)
    # For the usual centred reference, WRF records the requested mother-domain
    # centre. Explicit ref_x/ref_y may move the geometric centre legitimately.
    center = config.get('center') or {}
    if domain['id'] == 'd01' and center.get('grid_x') is None and center.get('grid_y') is None:
        for attr, key in (('CEN_LAT', 'lat'), ('CEN_LON', 'lon')):
            if key in center and not np.isclose(float(getattr(data, attr)), float(center[key]), rtol=0, atol=1e-4):
                raise ValueError('domain_center_mismatch:' + attr)


def validate(root, config):
    start = dt.datetime.fromisoformat(config['time_range']['product_start'].replace('Z', '+00:00')).replace(tzinfo=None)
    time_range = config['time_range']
    end = dt.datetime(*(int(time_range['end_' + name]) for name in ('year', 'month', 'day', 'hour')),
                      minute=int(time_range.get('end_minute', 0)), second=int(time_range.get('end_second', 0)))
    model_start = dt.datetime.fromisoformat(time_range.get('model_start',time_range['product_start']).replace('Z','+00:00')).replace(tzinfo=None)
    previews = (config.get('namelist_plan') or {}).get('previews',{})
    control = f90nml.reads(previews['input']).get('time_control',{}) if previews.get('input') else {}
    projection = f90nml.reads(previews['wps']).get('geogrid', {}) if previews.get('wps') else {}
    expected = {}
    for index,domain in enumerate(config['domains']):
        interval, begin, end_offset = history_settings(control, index,
            int(time_range['history_interval_minutes']), int(time_range.get('spinup_hours', 0)) * 60)
        if interval <= 0: raise ValueError('invalid_output_interval')
        finish = min(end,model_start+dt.timedelta(seconds=end_offset))
        current=model_start+dt.timedelta(seconds=begin)
        expected[domain['id']] = set()
        while current <= finish:
            if current >= start: expected[domain['id']].add(current.strftime('%Y-%m-%d_%H:%M:%S'))
            current += dt.timedelta(seconds=interval)
        if not expected[domain['id']]: raise ValueError('empty_output_schedule:'+domain['id'])
    found = {d['id']: set() for d in config['domains']}
    valid, invalid = [], []
    for path in sorted(root.glob('wrfout_d*_*')):
        item = {'name': path.name, 'size': path.stat().st_size}
        try:
            before = (path.stat().st_size, path.stat().st_mtime_ns)
            with netCDF4.Dataset(str(path)) as data:
                missing = set(REQUIRED) - set(data.variables)
                if missing:
                    raise ValueError('missing_variables:' + ','.join(sorted(missing)))
                domain = 'd%02d' % int(data.GRID_ID)
                if domain not in found:
                    raise ValueError('unexpected_domain')
                geometry = next(item for item in config['domains'] if item['id']==domain)
                if not np.isclose(float(data.DX),geometry['dx']) or not np.isclose(float(data.DY),geometry['dy']):
                    raise ValueError('grid_spacing_mismatch')
                if len(data.dimensions['west_east']) != geometry['e_we']-1 or len(data.dimensions['south_north']) != geometry['e_sn']-1:
                    raise ValueError('grid_shape_mismatch')
                if int(data.MAP_PROJ) not in (1, 2, 3):
                    raise ValueError('unsupported_projection')
                validate_geometry(data, geometry, config, projection)
                for attr in ('DX', 'DY', 'CEN_LAT', 'CEN_LON', 'TRUELAT1', 'STAND_LON'):
                    if not np.isfinite(float(getattr(data, attr))):
                        raise ValueError('invalid_projection_metadata:' + attr)
                times = netCDF4.chartostring(data.variables['Times'][:]).tolist()
                if isinstance(times, str): times = [times]
                for name in REQUIRED[1:]:
                    variable = data.variables[name]
                    for index in range(variable.shape[0]):
                        values = variable[index]
                        if np.ma.isMaskedArray(values) and np.ma.getmaskarray(values).any():
                            raise ValueError('missing_values:' + name)
                        if not np.isfinite(values).all():
                            raise ValueError('non_finite_values:' + name)
                        if name == 'XLAT' and (np.min(values) < -90 or np.max(values) > 90):
                            raise ValueError('invalid_latitude')
                        if name == 'XLONG' and (np.min(values) < -180 or np.max(values) > 180):
                            raise ValueError('invalid_longitude')
                if found[domain].intersection(times):
                    raise ValueError('duplicate_times')
                found[domain].update(times)
            if (path.stat().st_size, path.stat().st_mtime_ns) != before:
                raise ValueError('still_writing')
            valid.append(dict(item, reason='ok'))
        except Exception as exc:
            invalid.append(dict(item, reason=str(exc)))
    gaps = {domain: sorted(expected[domain] - values) for domain, values in found.items() if expected[domain] - values}
    return {'schema': 'wrf-output-validation-1', 'valid': valid, 'invalid': invalid,
            'missing_times': gaps, 'complete': bool(valid) and not invalid and not gaps}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path); parser.add_argument('config', type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.root, json.loads(args.config.read_text()))))
