"""Lease reclamation never infers terminal state from missing directories."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from compute_lease import reclaim


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.task = 'wrf_gfs_20261007T030000Z_c964e293'
        self.bound = {'task_id': self.task, 'control': str(self.root/'tasks'/self.task/'control'),
                      'attempt': 'a'*32, 'deletion_id': 'delete_'+'b'*32, 'deletion_digest': 'c'*64,
                      'terminal': {'verified': True, 'status': 'cancelled', 'attempt': 'a'*32,
                                   'cancellation_verified': True}}
        (self.root/'compute.lock').touch()
        self.lease = self.root/'compute-lease.json'
        self.lease.write_text(json.dumps({'root': self.bound['control'], 'attempt': 'a'*32}))

    def test_read_only_then_atomic_archive_preserves_original(self):
        original = self.lease.read_bytes()
        with patch('compute_lease.no_live_processes'):
            reclaim(self.root,self.bound)
            self.assertEqual(self.lease.read_bytes(),original)
            self.assertFalse((self.root/'.compute-lease-audit').exists())
            result = reclaim(self.root,self.bound,True)
        self.assertEqual(result['status'],'archived')
        self.assertFalse(self.lease.exists())
        self.assertEqual((self.root/'.compute-lease-audit'/('delete_'+'b'*32+'.lease.json')).read_bytes(),original)
        with self.assertRaises(FileNotFoundError):
            reclaim(self.root,self.bound,True)

    def test_missing_proof_changed_lease_or_replaced_root_cannot_release(self):
        for change in ('proof','lease','root','live'):
            with self.subTest(change=change):
                if change=='proof': self.bound['terminal']['verified']=False
                elif change=='lease': self.bound['attempt']='d'*32
                elif change=='root': Path(self.bound['control']).parent.mkdir(parents=True)
                try:
                    with patch('compute_lease.no_live_processes',side_effect=ValueError('live')):
                        with self.assertRaises(ValueError):reclaim(self.root,self.bound,True)
                    self.assertTrue(self.lease.exists())
                finally:
                    self.bound['terminal']['verified']=True; self.bound['attempt']='a'*32
                    if change=='root':Path(self.bound['control']).parent.rmdir()

    def test_private_publication_field_rejects_non_boolean(self):
        from schemas import WrfTaskCreate
        from test_namelist_config import request
        for value in ('false', 0, None):
            with self.assertRaises(ValueError):WrfTaskCreate(**dict(request(), publish_to_catalog=value))
        self.assertIs(WrfTaskCreate(**dict(request(), publish_to_catalog=False)).publish_to_catalog,False)
