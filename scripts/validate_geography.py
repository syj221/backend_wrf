"""Read-only default WPS geography gate; tile payloads are never read.

This contract supports complete, global regular_ll binary datasets. Other
mandatory layouts require an explicit validator extension, never an optimistic
readiness result. Optional GEOGRID sources are recorded without being promoted
to mandatory sources. Selection follows WPS 4.6.0 source_data_module.F; larger
numeric priorities are attempted first by process_tile_module.F.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat


SCHEMA = 'wrf-geography-1'
TILE_NAME = re.compile(r'[0-9]{5,6}-[0-9]{5,6}\.[0-9]{5,6}-[0-9]{5,6}')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def assignments(text):
    """Split comments and semicolon assignments outside quoted strings."""
    for line in text.splitlines():
        quote = None
        part = []
        pieces = []
        for character in line:
            if character in {'"', "'"}:
                if quote is None:
                    quote = character
                elif quote == character:
                    quote = None
            if quote is None and character == '#':
                break
            if quote is None and character == ';':
                pieces.append(''.join(part).strip())
                part = []
            else:
                part.append(character)
        if quote is not None:
            raise ValueError('Unterminated geography metadata quote')
        pieces.append(''.join(part).strip())
        for piece in pieces:
            if piece:
                yield piece


def scalar(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_table(text):
    blocks, current = [], {}
    for piece in assignments(text):
        if set(piece) == {'='}:
            if current:
                blocks.append(current)
                current = {}
            continue
        if '=' not in piece:
            raise ValueError('Unsupported GEOGRID table assignment')
        key, value = piece.split('=', 1)
        current.setdefault(key.strip().lower(), []).append(scalar(value.strip()))
    if current:
        blocks.append(current)
    entries = []
    priorities = {}
    for block in blocks:
        def single(key, default=None):
            values = block.get(key, [default])
            if len(values) != 1 or values[0] is None:
                raise ValueError('Missing or duplicate GEOGRID key: ' + key)
            return values[0]
        name = single('name')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name):
            raise ValueError('Invalid GEOGRID field name')
        priority = integer(single('priority'), 'priority', minimum=1)
        optional = single('optional', 'no').lower()
        if optional not in {'yes', 'no'}:
            raise ValueError('Unsupported GEOGRID optional value: ' + name)
        prior = priorities.setdefault(name, [])
        if priority in prior:
            raise ValueError('Duplicate GEOGRID field priority: ' + name)
        prior.append(priority)
        selected = {}
        for key in ('rel_path', 'abs_path', 'interp_option'):
            for value in block.get(key, []):
                resolution, payload = value.split(':', 1) if ':' in value else ('default', value)
                resolution, payload = resolution.strip(), payload.strip()
                if not resolution or not payload:
                    raise ValueError('Empty GEOGRID resolution or value: ' + name)
                identity = (('path' if key != 'interp_option' else 'interp'), resolution)
                if identity in selected:
                    raise ValueError('Duplicate GEOGRID resolution mapping: ' + name)
                selected[identity] = (key, payload)
        path_kind, path = selected.get(('path', 'default'), (None, None))
        interpolation = selected.get(('interp', 'default'), (None, None))[1]
        if optional == 'no' and (path is None or interpolation is None):
            raise ValueError('Mandatory geography has no default path/interpolator: ' + name)
        entries.append({'field': name, 'priority': priority, 'optional': optional == 'yes',
                        'path_kind': path_kind, 'path': path, 'interpolation': interpolation})
    for name, values in priorities.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            raise ValueError('Nonconsecutive GEOGRID priorities: ' + name)
    if not entries or not any(not entry['optional'] for entry in entries):
        raise ValueError('No mandatory default geography sources')
    return sorted(entries, key=lambda entry: (entry['field'], -entry['priority']))


def integer(value, name, minimum=0):
    if not re.fullmatch(r'[+-]?[0-9]+', str(value)) or int(value) < minimum:
        raise ValueError('Invalid geography integer: ' + name)
    return int(value)


def number(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid geography number: ' + name) from exc
    if not math.isfinite(result):
        raise ValueError('Nonfinite geography number: ' + name)
    return result


def regular_file(name, directory=None):
    descriptor = os.open(str(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Geography file is not regular: ' + str(name))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, info


def small_file(name, directory=None):
    descriptor, before = regular_file(name, directory)
    try:
        if before.st_size > 1024 * 1024:
            raise ValueError('Geography metadata exceeds size limit: ' + str(name))
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(1024 * 1024 + 1)
        after = os.fstat(descriptor)
        current = os.stat(str(name), dir_fd=directory, follow_symlinks=False)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current) or len(data) != before.st_size:
            raise ValueError('Geography metadata changed while reading: ' + str(name))
        return data
    finally:
        os.close(descriptor)


def dataset_directory(root, relative):
    relative = PurePosixPath(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts or '\\' in str(relative):
        raise ValueError('Unsafe mandatory geography path: ' + str(relative))
    descriptor = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts:
            nested = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = nested
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def index_metadata(text):
    values = {}
    for piece in assignments(text):
        if '=' not in piece:
            raise ValueError('Unsupported geography index assignment')
        key, value = piece.split('=', 1)
        key = key.strip().lower()
        if key in values:
            raise ValueError('Duplicate geography index key: ' + key)
        values[key] = scalar(value.strip())
    if values.get('projection') != 'regular_ll' or values.get('type') not in {'continuous', 'categorical'}:
        raise ValueError('Unsupported mandatory geography projection/type')
    required = ('dx', 'dy', 'known_x', 'known_y', 'known_lat', 'known_lon', 'wordsize', 'tile_x', 'tile_y')
    if any(key not in values for key in required):
        raise ValueError('Mandatory geography index lacks required metadata')
    result = {key: number(values[key], key) for key in required[:6]}
    result.update({key: integer(values[key], key, minimum=1) for key in required[6:]})
    result['projection'], result['type'] = values['projection'], values['type']
    result['tile_bdr'] = integer(values.get('tile_bdr', '0'), 'tile_bdr')
    if 'tile_z_start' in values or 'tile_z_end' in values:
        if 'tile_z' in values or not {'tile_z_start', 'tile_z_end'}.issubset(values):
            raise ValueError('Ambiguous geography vertical tile range')
        start = integer(values['tile_z_start'], 'tile_z_start', minimum=1)
        end = integer(values['tile_z_end'], 'tile_z_end', minimum=start)
    else:
        start, end = 1, integer(values.get('tile_z', ''), 'tile_z', minimum=1)
    result.update(tile_z_start=start, tile_z_end=end)
    if result['wordsize'] not in {1, 2, 3, 4}:
        raise ValueError('Unsupported geography wordsize')
    if values.get('row_order', 'bottom_top') not in {'bottom_top', 'top_bottom'}:
        raise ValueError('Unsupported geography row order')
    result['row_order'] = values.get('row_order', 'bottom_top')
    result['signed'] = values.get('signed', 'no')
    result['endian'] = values.get('endian', 'big')
    if result['signed'] not in {'yes', 'no'} or result['endian'] not in {'big', 'little'}:
        raise ValueError('Unsupported geography signedness/byte order')
    result['filename_digits'] = integer(values.get('filename_digits', '5'), 'filename_digits', minimum=1)
    if result['filename_digits'] not in {5, 6}:
        raise ValueError('Unsupported geography filename digits')
    result['scale_factor'] = number(values.get('scale_factor', '1'), 'scale_factor')
    result['missing_value'] = number(values['missing_value'], 'missing_value') if 'missing_value' in values else None
    if result['type'] == 'categorical':
        if not {'category_min', 'category_max'}.issubset(values):
            raise ValueError('Categorical geography lacks category bounds')
        result['category_min'] = integer(values['category_min'], 'category_min', minimum=-2147483648)
        result['category_max'] = integer(values['category_max'], 'category_max', minimum=result['category_min'])
    if result['known_x'] != 1 or result['known_y'] != 1 or not -360 <= result['known_lon'] <= 360:
        raise ValueError('Unsupported geography reference origin')
    for axis, extent in (('x', 360), ('y', 180)):
        spacing = abs(result['d' + axis])
        if not spacing or spacing > extent:
            raise ValueError('Invalid geography grid spacing')
        count = round(extent / spacing)
        if count > 1000000 or abs(count * spacing - extent) > spacing * 0.05 or count % result['tile_' + axis]:
            raise ValueError('Mandatory geography is not a supported complete global tile layout')
        result['global_n' + axis] = count
        if count >= 10 ** result['filename_digits']:
            raise ValueError('Geography grid exceeds configured filename digits')
    expected_lat = (-90 if result['dy'] > 0 else 90) + result['dy'] / 2
    if abs(result['known_lat'] - expected_lat) > abs(result['dy']) * 0.01:
        raise ValueError('Mandatory geography latitude coverage is not global')
    return result


def validate_dataset(root, entry):
    if entry['path_kind'] != 'rel_path':
        raise ValueError('Unsupported absolute mandatory geography source: ' + entry['field'])
    relative = str(PurePosixPath(entry['path']))
    directory = dataset_directory(root, relative)
    try:
        raw = small_file('index', directory)
        metadata = index_metadata(raw.decode('utf-8'))
        nx, ny = metadata['global_nx'], metadata['global_ny']
        tx, ty = metadata['tile_x'], metadata['tile_y']
        if (nx // tx) * (ny // ty) > 200000:
            raise ValueError('Mandatory geography tile inventory exceeds bounded gate')
        width = metadata['filename_digits']
        expected = {('{0:0{4}d}-{1:0{4}d}.{2:0{4}d}-{3:0{4}d}'.format(x, x + tx - 1, y, y + ty - 1, width))
                    for x in range(1, nx + 1, tx) for y in range(1, ny + 1, ty)}
        actual = {name for name in os.listdir(directory) if TILE_NAME.fullmatch(name)}
        if actual != expected:
            raise ValueError('Geography tile inventory differs: ' + entry['field'] +
                             ' missing=' + str(len(expected - actual)) + ' extra=' + str(len(actual - expected)))
        size = (tx + 2 * metadata['tile_bdr']) * (ty + 2 * metadata['tile_bdr']) * (metadata['tile_z_end'] - metadata['tile_z_start'] + 1) * metadata['wordsize']
        inventory = {}
        for name in sorted(expected):
            descriptor, info = regular_file(name, directory)
            try:
                if info.st_size != size:
                    raise ValueError('Geography tile byte size differs: ' + entry['field'] + '/' + name)
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                    raise ValueError('Geography tile changed while checking: ' + entry['field'] + '/' + name)
                inventory[name] = info.st_size
            finally:
                os.close(descriptor)
        return {'field': entry['field'], 'priority': entry['priority'], 'relative_path': relative,
                'interpolation': entry['interpolation'], 'index_sha256': hashlib.sha256(raw).hexdigest(),
                'metadata': metadata, 'tile_inventory_sha256': digest(inventory),
                'tile_count': len(inventory), 'logical_bytes': sum(inventory.values())}
    finally:
        os.close(directory)


def validate_geography(table, geog_root, expected_contract=None):
    table = Path(table)
    root = Path(geog_root).resolve(strict=True)
    raw = small_file(table)
    entries = parse_table(raw.decode('utf-8'))
    datasets = [validate_dataset(root, entry) for entry in entries if not entry['optional']]
    optional = [{key: entry[key] for key in ('field', 'priority', 'path_kind', 'path', 'interpolation')}
                for entry in entries if entry['optional']]
    contract = {'schema': SCHEMA, 'resolution': 'default', 'geog_root': str(root),
                'table_sha256': hashlib.sha256(raw).hexdigest(), 'datasets': datasets,
                'optional_sources_not_required': optional, 'tile_payloads_read': False,
                'tile_count': sum(item['tile_count'] for item in datasets),
                'logical_bytes': sum(item['logical_bytes'] for item in datasets)}
    contract['digest'] = digest(contract)
    if expected_contract is not None and contract != expected_contract:
        raise ValueError('Frozen geography contract changed')
    return contract


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--table', required=True, type=Path)
    parser.add_argument('--geog-root', required=True, type=Path)
    parser.add_argument('--expected-contract', type=Path)
    args = parser.parse_args()
    expected = json.loads(args.expected_contract.read_text()) if args.expected_contract else None
    print(json.dumps(validate_geography(args.table, args.geog_root, expected), sort_keys=True))
