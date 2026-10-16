"""Regression for WPS sequential namelist reads; never starts WPS or WRF."""
import hashlib
import importlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import f90nml
from namelist_config import resolve, source_catalog
from scripts.namelist_layout import ordered_groups, validate_layout
from test_namelist_config import request, config


class LayoutTest(unittest.TestCase):
    def test_quilt_is_generated_as_fixed_scalar_io_configuration(self):
        resolved = resolve(request())
        parsed = f90nml.reads(resolved['previews']['input'])
        self.assertEqual(dict(parsed['namelist_quilt']),
                         {'nio_tasks_per_group': 0, 'nio_groups': 1, 'poll_servers': False})
        self.assertEqual(list(parsed)[-1], 'namelist_quilt')
        r = request()
        r['namelist'] = config(input={'namelist_quilt': {'nio_tasks_per_group': 1}})
        with self.assertRaisesRegex(ValueError, '未开放'):
            resolve(r)

    def test_runtime_rejects_missing_quilt_before_writing_and_accepts_new_plan(self):
        with patch.object(sys, 'path', [str(Path(__file__).parent/'scripts'), *sys.path]):
            writer = importlib.import_module('apply_namelist_plan')
        resolved = resolve(request())
        original = resolved['previews']['input']
        legacy = re.sub(r'(?ms)^&namelist_quilt\b.*?^/\s*', '', original)
        snapshot = {k: source_catalog()[k] for k in ('source_hash', 'binary_hashes')}
        plan = {**snapshot, 'digest': resolved['digest'], 'previews': {'input': legacy},
                'preview_hashes': {'input': hashlib.sha256(legacy.encode()).hexdigest()}}
        with TemporaryDirectory(prefix='quilt-runtime-') as tmp:
            target = Path(tmp)/'namelist.input'
            target.write_text('preserved old file')
            env = {'WRF_SOURCE_DIR': tmp, 'WPS_SOURCE_DIR': tmp,
                   'WRF_NUM_METGRID_LEVELS': '34', 'WRF_NUM_METGRID_SOIL_LEVELS': '4'}
            with patch.object(writer, 'snapshot', return_value=snapshot), patch.dict(os.environ, env):
                with self.assertRaisesRegex(ValueError, '缺少必要配置段: namelist_quilt'):
                    writer.apply({'namelist_plan': plan}, 'input', target)
                self.assertEqual(target.read_text(), 'preserved old file')
                self.assertFalse(target.with_suffix('.input.plan.json').exists())
                plan['previews']['input'] = original
                plan['preview_hashes']['input'] = hashlib.sha256(original.encode()).hexdigest()
                writer.apply({'namelist_plan': plan}, 'input', target)
                parsed = f90nml.read(target)
                self.assertEqual(parsed['domains']['num_metgrid_levels'], 34)
                self.assertEqual(parsed['namelist_quilt']['nio_tasks_per_group'], 0)

    @unittest.skipUnless(shutil.which('gfortran'), 'gfortran required for executable reader test')
    def test_mpi_quilt_read_accepts_generated_file_and_rejects_missing_group(self):
        # Match the mandatory READ in WRF frame/module_io_quilt_old.F.
        source = '''program quilt_reader
implicit none
integer :: nio_tasks_per_group, nio_groups, io_status
logical :: poll_servers
namelist /namelist_quilt/ nio_tasks_per_group, nio_groups, poll_servers
nio_groups = 1
nio_tasks_per_group = 0
poll_servers = .false.
open(unit=27,file='namelist.input',status='old')
read(27,nml=namelist_quilt,iostat=io_status)
if (io_status /= 0) stop 1
if (nio_tasks_per_group /= 0 .or. nio_groups /= 1 .or. poll_servers) stop 2
close(27)
end program quilt_reader
'''
        text = resolve(request())['previews']['input']
        with TemporaryDirectory(prefix='quilt-fortran-') as tmp:
            root = Path(tmp)
            (root/'reader.f90').write_text(source)
            compiled = subprocess.run([shutil.which('gfortran'), 'reader.f90', '-o', 'reader'],
                                      cwd=root, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            for valid, content in ((True, text), (False, re.sub(r'(?ms)^&namelist_quilt\b.*?^/\s*', '', text))):
                (root/'namelist.input').write_text(content)
                result = subprocess.run([str(root/'reader')], cwd=root, capture_output=True, text=True)
                with self.subTest(valid=valid):
                    self.assertEqual(result.returncode, 0 if valid else 1, result.stdout + result.stderr)

    def test_explicit_order_survives_json_key_sorting_and_conditional_groups(self):
        r = request()
        r['assimilation_scheme'] = 'fdda_standard'
        r['namelist'] = config(input={'physics': {'sf_surface_physics': 4}})
        for value in (r, json.loads(json.dumps(r, sort_keys=True))):
            resolved = resolve(value)
            for phase in ('wps', 'input'):
                text = resolved['previews'][phase]
                validate_layout(text, phase)
                self.assertEqual(list(f90nml.reads(text)), ordered_groups(phase, resolved['effective'][phase]))
            self.assertIn('&noah_mp', resolved['previews']['input'])
            self.assertIn('&fdda', resolved['previews']['input'])

    def test_order_missing_group_duplicate_and_terminator_are_rejected(self):
        text = resolve(request())['previews']['wps']
        parsed = f90nml.reads(text)
        old = str(f90nml.Namelist(dict(parsed)))
        self.assertTrue(old.startswith('&geogrid'))
        for invalid in (old, text.replace('&share','&missing'), text + '\n&share\n/\n', text.replace('/', '', 1)):
            with self.subTest(invalid=invalid[:20]), self.assertRaisesRegex(ValueError, 'NAMELIST_LAYOUT_ERROR'):
                validate_layout(invalid, 'wps')
        validate_layout(text.replace('@GEOG_DATA_PATH@', '/managed/share/geogrid/'), 'wps')

    def test_old_frozen_digest_requires_explicit_reconfirmation(self):
        r = request(); resolved = resolve(r)
        old = hashlib.sha256(json.dumps([resolved['effective'], source_catalog()['source_hash']], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        without_quilt = json.loads(json.dumps(resolved['effective']))
        without_quilt['input'].pop('namelist_quilt')
        layout_one = hashlib.sha256(json.dumps([without_quilt, source_catalog()['source_hash'],
            'wrf-namelist-layout-1'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        for digest in (old, layout_one):
            with self.subTest(digest=digest):
                self.assertNotEqual(digest, resolved['digest'])
                r['namelist'] = {**config(), 'plan_digest': digest}
                with self.assertRaisesRegex(ValueError, '重新确认计划'):
                    resolve(r)

    def test_runtime_rejects_hash_valid_but_bad_layout_without_overwriting(self):
        with patch.object(sys, 'path', [str(Path(__file__).parent/'scripts'), *sys.path]):
            writer = importlib.import_module('apply_namelist_plan')
        original = resolve(request())['previews']['wps']
        legacy = str(f90nml.Namelist(dict(f90nml.reads(original))))
        snapshot = {k: source_catalog()[k] for k in ('source_hash','binary_hashes')}
        plan = {**snapshot, 'digest':'fixture', 'previews':{'wps':legacy},
                'preview_hashes':{'wps':hashlib.sha256(legacy.encode()).hexdigest()}}
        with TemporaryDirectory(prefix='layout-runtime-') as tmp:
            target = Path(tmp)/'namelist.wps'; target.write_text('existing file')
            with patch.object(writer,'snapshot',return_value=snapshot), patch.dict(os.environ, {'WRF_SOURCE_DIR':tmp,'WPS_SOURCE_DIR':tmp,'GEOG_DATA_PATH':'/managed/geog'}):
                with self.assertRaisesRegex(ValueError,'NAMELIST_LAYOUT_ERROR'):
                    writer.apply({'namelist_plan':plan},'wps',target)
                self.assertEqual(target.read_text(),'existing file')
                self.assertFalse(target.with_suffix('.wps.plan.json').exists())
                plan['previews']['wps'] = original
                plan['preview_hashes']['wps'] = hashlib.sha256(original.encode()).hexdigest()
                writer.apply({'namelist_plan':plan},'wps',target)
                validate_layout(target.read_text(),'wps')
                self.assertEqual(f90nml.read(target)['geogrid']['geog_data_path'],'/managed/geog')

    @unittest.skipUnless(shutil.which('gfortran'), 'gfortran required for executable reader test')
    def test_actual_fortran_reads_generated_files_and_reproduces_legacy_eof(self):
        """Declare all generated variables, then READ groups on one unit."""
        r = request(); r['assimilation_scheme'] = 'fdda_standard'
        r['domains'].append({'id':'d02','dx':3000,'dy':3000,'e_we':61,'e_sn':61,
                             'parent_id':1,'parent_grid_ratio':3,'i_parent_start':11,'j_parent_start':11})
        r['namelist'] = config(input={'physics':{'sf_surface_physics':4}})
        resolved = resolve(r)
        types = {f['id']: f['type'] for f in source_catalog()['fields']}
        with TemporaryDirectory(prefix='layout-fortran-') as tmp:
            root = Path(tmp)
            for phase in ('wps','input'):
                values = resolved['effective'][phase]
                groups = ordered_groups(phase, values)
                declarations = {}
                for group, fields in values.items():
                    for name, value in fields.items():
                        items = value if isinstance(value,list) else [value]
                        if not items: continue
                        sample = next(v for v in items if v is not None)
                        # Registry real defaults can be represented as Python ints.
                        kind = types.get(f'{phase}.{group}.{name}')
                        typ = {'boolean':'logical','integer':'integer','number':'real(kind=8)','string':'character(len=4096)'}.get(kind)
                        if typ is None:
                            typ = 'logical' if isinstance(sample,bool) else 'integer' if isinstance(sample,int) else 'real(kind=8)' if isinstance(sample,float) else 'character(len=4096)'
                        shape = f'({len(items)})' if isinstance(value,list) else ''
                        declaration = f'{typ} :: {name}{shape}'
                        self.assertIn(declarations.get(name,declaration), (declaration,))
                        declarations[name] = declaration
                source = ['program reader','implicit none','integer :: unit, status','character(len=1024) :: error_message', *declarations.values()]
                source += [f'namelist /{g}/ '+', '.join(n for n in values[g] if n in declarations) for g in groups]
                source += ["open(newunit=unit,file='fixture.nml',status='old')"]
                for group in groups:
                    source += [f'read(unit,nml={group},iostat=status,iomsg=error_message)', 'if (status /= 0) then', f"print *, '{group}: ', trim(error_message)", 'stop 1', 'end if']
                source += ['close(unit)','end program reader']
                (root/'reader.f90').write_text('\n'.join(source)+'\n')
                compiled = subprocess.run([shutil.which('gfortran'),'-ffree-line-length-none','reader.f90','-o','reader'],cwd=root,capture_output=True,text=True)
                self.assertEqual(compiled.returncode,0,compiled.stderr)
                (root/'fixture.nml').write_text(resolved['previews'][phase])
                good = subprocess.run([str(root/'reader')],cwd=root,capture_output=True,text=True)
                self.assertEqual(good.returncode,0,phase+' '+good.stdout+good.stderr)
                if phase == 'wps':
                    (root/'fixture.nml').write_text(str(f90nml.Namelist(dict(values))))
                    bad = subprocess.run([str(root/'reader')],cwd=root,capture_output=True,text=True)
                    self.assertNotEqual(bad.returncode,0,'Old alphabetic layout must fail a forward-only read')


if __name__ == '__main__':
    unittest.main()
