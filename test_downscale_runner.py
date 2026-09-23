from __future__ import annotations

import io
import importlib.util
import tarfile
import tempfile
import unittest
from pathlib import Path

from downscale.runner import DownscaleRunner


CFD_CHAIN_PATH = Path(__file__).parent / "scripts" / "wrf_hpc_cfd_10m_chain.py"
CFD_CHAIN_SPEC = importlib.util.spec_from_file_location("wrf_hpc_cfd_10m_chain", CFD_CHAIN_PATH)
cfd_chain_module = importlib.util.module_from_spec(CFD_CHAIN_SPEC)
assert CFD_CHAIN_SPEC and CFD_CHAIN_SPEC.loader
CFD_CHAIN_SPEC.loader.exec_module(cfd_chain_module)
CFD_CASE_PATH = Path(__file__).parent / "scripts" / "wrf_hpc_cfd_case.py"
CFD_CASE_SPEC = importlib.util.spec_from_file_location("wrf_hpc_cfd_case", CFD_CASE_PATH)
cfd_case_module = importlib.util.module_from_spec(CFD_CASE_SPEC)
assert CFD_CASE_SPEC and CFD_CASE_SPEC.loader
CFD_CASE_SPEC.loader.exec_module(cfd_case_module)


class DownscaleBundleExtractionTests(unittest.TestCase):
    def test_generated_flat_surface_is_closed_and_open_surface_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            surface = Path(temporary) / "flat.stl"
            cfd_case_module.write_flat_prism(surface, 1100, 1100)
            self.assertEqual(cfd_case_module.validate_closed_stl(surface), 12)
            surface.write_text("solid open\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid open\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "at least four triangles|not a closed"):
                cfd_case_module.validate_closed_stl(surface)

    def test_10m_cfd_runner_writes_height_export_functions(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary) / "case"
            (case / "system").mkdir(parents=True)
            chain = cfd_chain_module.Cfd10mChain.__new__(cfd_chain_module.Cfd10mChain)
            chain.heights = [10, 40]
            path = chain.write_export_dictionary(case)
            content = path.read_text(encoding="utf-8")
            self.assertIn("z10Products", content)
            self.assertIn("z40Products", content)
            self.assertIn("z40_products", content)

    def test_extracts_tar_directories_and_regular_product_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bundle.tar.gz"
            source = root / "source"
            (source / "preview" / "40").mkdir(parents=True)
            (source / "downscale.product.json").write_text("{}", encoding="utf-8")
            (source / "preview" / "40" / "wind_speed.webp").write_bytes(b"webp")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(source / "downscale.product.json", arcname="downscale.product.json")
                tar.add(source / "preview", arcname="preview")

            target = root / "target"
            target.mkdir()
            DownscaleRunner._extract_bundle(archive, target)
            self.assertEqual((target / "downscale.product.json").read_text(encoding="utf-8"), "{}")
            self.assertEqual((target / "preview" / "40" / "wind_speed.webp").read_bytes(), b"webp")

    def test_rejects_path_traversal_and_symlink_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            for kind in ("traversal", "symlink"):
                archive = root / f"{kind}.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    if kind == "traversal":
                        member = tarfile.TarInfo("../escaped.json")
                        member.size = 2
                        tar.addfile(member, io.BytesIO(b"{}"))
                    else:
                        member = tarfile.TarInfo("scene.meta.json")
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/etc/passwd"
                        tar.addfile(member)
                with self.assertRaisesRegex(ValueError, "非法|非普通"):
                    DownscaleRunner._extract_bundle(archive, target)
            self.assertFalse((root / "escaped.json").exists())



class AutoUpstreamRunnerTests(unittest.TestCase):
    def setup_runner(self, root, state):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from downscale.schemas import DownscaleValidationRequest
        from downscale.store import DownscaleStore
        request = DownscaleValidationRequest(start_time='2026-09-17T06:00:00Z', end_time='2026-09-17T12:00:00Z', aoi={'center_lat':32.06,'center_lon':118.8,'horizontal_crs':'EPSG:32650','width_m':500,'height_m':500}, cfd={'rans':{'selected_hours':[0,6]}})
        store = DownscaleStore(root/'tasks.sqlite3')
        self.addCleanup(store.close)
        task = store.create('ds_test','user',request.model_dump(mode='json'),{'source_wrf':{'task_id':'wrf_child','owner_role':1}})
        service = SimpleNamespace(store=store,normal_task_lookup=Mock(return_value={'id':'wrf_child','status':state}),source_artifact_lookup=Mock(return_value={'files':[]}),_validate_source_compatibility=Mock())
        hpc = Mock()
        runner = DownscaleRunner(SimpleNamespace(run_dir=root,downscale_runner_poll_seconds=2),service,hpc)
        return runner, task, service, hpc

    def test_waiting_child_does_not_launch_remote_or_starve_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, task, service, hpc = self.setup_runner(Path(temporary),'running')
            runner._execute(task)
            self.assertEqual(service.store.get(task['id'])['status'],'waiting_upstream')
            self.assertIsNone(runner._next_task())
            hpc.launch_downscale.assert_not_called()

    def test_terminal_child_failure_reaches_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, task, service, hpc = self.setup_runner(Path(temporary),'failed')
            runner._execute(task)
            self.assertEqual(service.store.get(task['id'])['stage'],'upstream_failed')
            self.assertEqual(service.store.get(task['id'])['status'],'failed')
            hpc.launch_downscale.assert_not_called()

    def test_ready_child_stages_exact_contract_path_and_launches_without_new_input(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, task, service, hpc = self.setup_runner(root,'succeeded')
            hpc.launch_downscale.return_value={'remote_pid':123}
            runner._stop.set()
            with patch('downscale.runner.build_source_artifact',return_value={'task_id':'wrf_child','files':[]}):
                runner._execute(task)
            expected=root/'downscale_jobs'/task['id']/'downscale.job.json'
            self.assertTrue(expected.is_file())
            self.assertFalse((expected.parent/task['id']).exists())
            self.assertEqual(hpc.prepare_downscale_runtime.call_args.args[1],expected)
            hpc.launch_downscale.assert_called_once_with(task['id'])
            service._validate_source_compatibility.assert_called_once()

    def test_old_remote_status_failure_is_reconciled_instead_of_relaunched(self):
        with tempfile.TemporaryDirectory() as temporary:
            runner, task, service, hpc = self.setup_runner(Path(temporary), 'succeeded')
            runtime = {"remote_launch_attempted": True, "source_wrf": {"task_id": "wrf_child", "owner_role": 1}}
            service.store.update(task['id'], status='failed', stage='remote_status_failed', runtime=runtime, error='ssh timeout')
            selected = runner._next_task()
            self.assertEqual(selected['id'], task['id'])
            self.assertEqual(selected['status'], 'reconciling')
            self.assertEqual(selected['stage'], 'remote_status_retrying')
            hpc.launch_downscale.assert_not_called()


if __name__ == "__main__":
    unittest.main()
