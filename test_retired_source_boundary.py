"""Post-retirement source and operator-path contracts use synthetic inputs."""
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
import ast
import unittest

from config import Settings
from hpc_transport import HpcClient
from retirement import (SCHEMA, LEGACY_ENVIRONMENT_SETTINGS, bind_legacy_settings,
                        legacy_task_paths, manifest_digest)

DS = "ds_20260918T120000Z_15711b1a"
PATHS = {
    "hpc_downscale_interface_root":"/managed/CFD/interface",
    "hpc_downscale_cfd_template":"/managed/CFD/templates/thermal-rans-graded-v2",
    "hpc_downscale_openfoam_activate":"/managed/CFD/activate.sh",
    "hpc_downscale_python":"/managed/ENV/wrfpost/bin/python",
}


class RetiredSourceBoundaryTest(unittest.TestCase):
    def manifest(self):
        value = {"schema":SCHEMA,"environment_ownership_review":[{"setting":key,"path":path} for key,path in PATHS.items()]}
        value["digest"] = manifest_digest(value)
        return value

    def test_product_settings_and_transport_have_no_legacy_execution_surface(self):
        names = {field.name for field in fields(Settings)}
        self.assertEqual({name for name in names if name.startswith('downscale_')},{'downscale_database_path','downscale_retired'})
        self.assertFalse(any(name.startswith('hpc_downscale_') for name in names))
        self.assertTrue(Settings().downscale_retired)
        for name in ('download_downscale_bundle','inspect_wrf_source','_assert_downscale_task_id',
                     'downscale_task_dir','downscale_output_dir','downscale_bundle_path',
                     'prepare_downscale_runtime','launch_downscale','cancel_downscale','downscale_status',
                     'inspect_downscale_forcing_manifest','analyze_geography'):
            self.assertFalse(hasattr(HpcClient,name),name)

    def test_main_has_no_legacy_package_or_regional_recommendation_import(self):
        tree = ast.parse((Path(__file__).parent/'main.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                self.assertNotIn((node.module or '').split('.')[0],{'downscale','recommendations'})
        names = {node.name for node in ast.walk(tree) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
        self.assertFalse(names & {'lookup_normal_wrf_task','lookup_normal_wrf_artifact','lookup_downscale_forcing_manifest',
                                 'submit_auto_normal_wrf_task','create_recommendation','recommendation_detail'})

    def test_operator_legacy_binding_uses_only_an_explicit_intact_inventory(self):
        settings = SimpleNamespace(hpc_remote_dir='/managed/RUNTIME',hpc_storage_alias='/managed')
        manifest = self.manifest()
        bound = bind_legacy_settings(settings,manifest)
        self.assertEqual({name:getattr(bound,name) for name in LEGACY_ENVIRONMENT_SETTINGS},PATHS)
        self.assertFalse(any(hasattr(settings,name) for name in LEGACY_ENVIRONMENT_SETTINGS))
        changed = deepcopy(manifest);changed['environment_ownership_review'][0]['path']='/different'
        with self.assertRaisesRegex(ValueError,'digest'):bind_legacy_settings(settings,changed)
        for entries in (manifest['environment_ownership_review'][:-1],
                        manifest['environment_ownership_review']+[manifest['environment_ownership_review'][0]],
                        [{'setting':'unexpected','path':'/managed'}]):
            changed = {**manifest,'environment_ownership_review':entries};changed['digest']=manifest_digest(changed)
            with self.assertRaises(ValueError):bind_legacy_settings(settings,changed)

    def test_retired_paths_are_id_bound_and_do_not_need_removed_hpc_methods(self):
        settings = SimpleNamespace(hpc_remote_dir='/managed/RUNTIME',hpc_storage_alias='/managed')
        control,output = legacy_task_paths(settings,DS)
        self.assertEqual(control,'/managed/RUNTIME/backend_wrf_downscale_tasks/'+DS)
        self.assertEqual(output,'/managed/WRFwork/DOWNSCALE_'+DS+'/run')
        for identity in ('wrf_gfs_20260918T120000Z_15711b1a',DS+'/../other','../'+DS):
            with self.assertRaises(ValueError):legacy_task_paths(settings,identity)


if __name__ == '__main__':
    unittest.main()
