"""Small isolated WPS geography fixtures; no scientific tiles or SSH."""
import copy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.validate_geography import parse_table, small_file, validate_geography


INDEX = '''type=continuous
projection=regular_ll
dx=90
dy=90
known_x=1
known_y=1
known_lat=-45
known_lon=-135
wordsize=1
tile_x=2
tile_y=2
tile_z=1
'''
TABLE = '''===============================
name=HGT_M
priority=1
dest_type=continuous
interp_option=default:four_pt
rel_path=default:global/
===============================
'''


def geography_fixture(base):
    root = base/'geog'
    folder = root/'global'
    folder.mkdir(parents=True)
    (folder/'index').write_text(INDEX)
    for name in ('00001-00002.00001-00002', '00003-00004.00001-00002'):
        (folder/name).write_bytes(bytes(4))
    return root


class GeographyValidationTest(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='wrf-geography-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = geography_fixture(self.base)
        self.table = self.base/'GEOGRID.TBL'
        self.table.write_text(TABLE)
        self.folder = self.root/'global'
        self.tile = self.folder/'00001-00002.00001-00002'

    def check(self, expected=None):
        return validate_geography(self.table, self.root, expected)

    def test_complete_global_tiles_are_bound_without_reading_payload(self):
        result = self.check()
        self.assertEqual(result['tile_count'], 2)
        self.assertEqual(result['logical_bytes'], 8)
        self.assertFalse(result['tile_payloads_read'])
        self.assertEqual(len(result['datasets'][0]['index_sha256']), 64)
        self.assertEqual(self.check(result), result)
        # This gate promises structure and size, not numerical payload hashes.
        self.tile.write_bytes(b'abcd')
        self.assertEqual(self.check(result), result)

    def test_negative_dy_is_a_valid_north_to_south_global_grid(self):
        index = self.folder/'index'
        index.write_text(INDEX.replace('dy=90', 'dy=-90').replace('known_lat=-45', 'known_lat=45'))
        result = self.check()
        self.assertEqual(result['datasets'][0]['metadata']['dy'], -90)
        self.assertEqual(result['tile_count'], 2)

    def test_missing_index_or_tile_and_truncated_tile_fail(self):
        index = self.folder/'index'
        index.unlink()
        with self.assertRaises(FileNotFoundError): self.check()
        index.write_text(INDEX)
        self.tile.unlink()
        with self.assertRaisesRegex(ValueError, 'tile inventory differs'): self.check()
        self.tile.write_bytes(b'\0')
        with self.assertRaisesRegex(ValueError, 'tile byte size differs'): self.check()

    def test_tile_index_and_nested_directory_links_are_refused(self):
        external = self.base/'external'
        external.write_bytes(bytes(4))
        self.tile.unlink();self.tile.symlink_to(external)
        with self.assertRaises(OSError): self.check()
        self.tile.unlink();self.tile.write_bytes(bytes(4))
        index = self.folder/'index'
        original = index.read_bytes();index.unlink();index.symlink_to(external)
        with self.assertRaises(OSError): self.check()
        index.unlink();index.write_bytes(original)
        (self.root/'alias').symlink_to(self.folder, target_is_directory=True)
        self.table.write_text(TABLE.replace('default:global/', 'default:alias/'))
        with self.assertRaises(OSError): self.check()

    def test_unknown_mandatory_layout_and_unsafe_paths_fail_closed(self):
        index = self.folder/'index'
        for modified in (INDEX.replace('regular_ll', 'lambert'),
                         INDEX.replace('known_lat=-45', 'known_lat=0'),
                         INDEX.replace('dx=90', 'dx=nan'),
                         INDEX.replace('wordsize=1', 'wordsize=8')):
            with self.subTest(index=modified):
                index.write_text(modified)
                with self.assertRaises(ValueError): self.check()
        index.write_text(INDEX)
        for path in ('../global/', '/outside/', 'global\\unsafe/'):
            with self.subTest(path=path):
                self.table.write_text(TABLE.replace('default:global/', 'default:'+path))
                with self.assertRaises(ValueError): self.check()
        self.table.write_text(TABLE.replace('rel_path=', 'abs_path='))
        with self.assertRaisesRegex(ValueError, 'absolute mandatory'): self.check()

    def test_optional_unknown_or_missing_sources_are_not_required(self):
        self.table.write_text(TABLE + '''name=VAR_SSO
priority=1
optional=yes
interp_option=default:four_pt
rel_path=default:missing_regional_source/
===============================
name=URB_PARAM
priority=1
optional=yes
interp_option=urban:four_pt
rel_path=urban:also_absent/
''')
        result = self.check()
        self.assertEqual(len(result['datasets']), 1)
        self.assertEqual(len(result['optional_sources_not_required']), 2)
        self.assertEqual(result['tile_count'], 2)

    def test_all_mandatory_priorities_need_data_in_descending_priority_order(self):
        self.table.write_text(TABLE + TABLE.replace('priority=1', 'priority=2'))
        result = self.check()
        self.assertEqual([entry['priority'] for entry in result['datasets']], [2, 1])
        self.table.write_text(TABLE + TABLE.replace('priority=1', 'priority=2').replace('default:global/', 'default:missing/'))
        with self.assertRaises(FileNotFoundError): self.check()
        for table in (TABLE + TABLE, TABLE.replace('priority=1', 'priority=2'),
                      TABLE.replace('rel_path=default:global/', 'rel_path=other:global/')):
            with self.subTest(table=table):
                self.table.write_text(table)
                with self.assertRaises(ValueError): self.check()

    def test_index_and_contract_drift_cannot_be_reapproved_by_readiness(self):
        result = self.check()
        changed = copy.deepcopy(result);changed['datasets'][0]['index_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'contract changed'): self.check(changed)
        (self.folder/'index').write_text(INDEX+'description="changed metadata"\n')
        with self.assertRaisesRegex(ValueError, 'contract changed'): self.check(result)

    def test_multilevel_vertical_range_border_and_wordsize_determine_exact_bytes(self):
        (self.folder/'index').write_text(INDEX.replace('wordsize=1', 'wordsize=2').replace('tile_z=1', 'tile_z_start=1\ntile_z_end=12\ntile_bdr=1'))
        for tile in self.folder.glob('0*'):
            tile.write_bytes(bytes(4*4*12*2))
        self.assertEqual(self.check()['logical_bytes'], 2*4*4*12*2)

    def test_category_scale_and_missing_metadata_are_validated_and_pinned(self):
        index = self.folder/'index'
        index.write_text(INDEX.replace('type=continuous','type=categorical')+
                         'category_min=1\ncategory_max=21\nscale_factor=0.01\nmissing_value=-999\nendian=little\nsigned=yes\n')
        metadata = self.check()['datasets'][0]['metadata']
        self.assertEqual((metadata['category_min'],metadata['category_max']),(1,21))
        self.assertEqual((metadata['scale_factor'],metadata['missing_value']),(0.01,-999))
        self.assertEqual((metadata['endian'],metadata['signed']),('little','yes'))
        for bad in ('category_min=22\ncategory_max=21', 'scale_factor=nan', 'missing_value=inf', 'filename_digits=4'):
            index.write_text(INDEX+'\n'+bad+'\n')
            if bad.startswith('category_min'):
                index.write_text(index.read_text().replace('type=continuous','type=categorical'))
            with self.subTest(metadata=bad):
                with self.assertRaises(ValueError): self.check()

    def test_partial_global_edge_tiles_require_an_explicit_layout_extension(self):
        (self.folder/'index').write_text(INDEX.replace('tile_x=2','tile_x=3'))
        with self.assertRaisesRegex(ValueError,'complete global tile layout'): self.check()

    def test_six_digit_tile_names_follow_index_setting(self):
        (self.folder/'index').write_text(INDEX+'filename_digits=6\n')
        for old,new in [('00001-00002.00001-00002','000001-000002.000001-000002'),
                        ('00003-00004.00001-00002','000003-000004.000001-000002')]:
            (self.folder/old).rename(self.folder/new)
        self.assertEqual(self.check()['tile_count'],2)

    def test_comments_and_semicolons_follow_table_assignment_syntax(self):
        entries = parse_table(TABLE.replace('priority=1', 'priority=1; optional=no # comment')
                              .replace('dest_type=continuous', 'dest_type=continuous; description="quoted; # text"'))
        self.assertEqual(entries[0]['field'], 'HGT_M')
        self.assertFalse(entries[0]['optional'])

    def test_atomic_metadata_path_replacement_is_not_mistaken_for_frozen_index(self):
        index = self.folder/'index'
        pending = self.folder/'replacement-index'
        pending.write_text(INDEX+'description="replacement"\n')
        fstat = os.fstat
        calls = 0
        def replace_after_read(descriptor):
            nonlocal calls
            info = fstat(descriptor)
            calls += 1
            if calls == 2:
                os.replace(pending,index)
            return info
        with patch('scripts.validate_geography.os.fstat',side_effect=replace_after_read):
            with self.assertRaisesRegex(ValueError,'metadata changed while reading'):
                small_file(index)
