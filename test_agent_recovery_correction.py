"""Compute-free scientific validation of task-bound agent corrections."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from database import TaskStore
from recovery import RecoveryService
from test_guidance_recovery import request
from test_recovery_diagnosis import runtime


class AgentCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(Path(":memory:"))
        self.addCleanup(self.store.close)
        original = request()
        original.update(
            configuration_profile="wrf-ordinary-ui-2", vertical_levels=20,
            publish_to_catalog=False,
        )
        self.store.create("task", original, "alice")
        self.store.update(
            "task", status="waiting_restart", stage="failed",
            failure={"failure_class": "configuration",
                     "message": "not enough eta levels to reach p_top"},
        )
        self.event = self.store.incidents("task")[0]
        self.manager = SimpleNamespace(
            store=self.store, get=self.store.get,
            diagnostic_evidence=Mock(return_value={
                "evidence": ["not enough eta levels to reach p_top"],
                "sources": [{"source": "local_log", "status": "available"}],
                "remote_status": "failed",
            }),
            restart_plan=Mock(return_value={
                "can_restart": True, "local_paths": ["fixture"],
                "remote_paths": ["fixture"],
            }), restart=Mock(),
        )
        self.service = RecoveryService(self.manager, runtime)

    def test_corrected_layers_produce_frozen_plan_without_changing_attempt(self):
        before = deepcopy(self.store.get("task"))
        events = deepcopy(self.store.incidents("task"))
        correction = {**before["request"], "vertical_levels": 88}
        plan = self.service.plan(
            "task", self.event["id"], "restart", correction, actor_sub="alice"
        )
        self.assertEqual(plan["status"], "pending")
        self.assertEqual(plan["request"]["vertical_levels"], 88)
        self.assertFalse(plan["request"]["publish_to_catalog"])
        self.assertTrue(plan["namelist_plan"]["execution_verified"])
        self.assertIn("e_vert", plan["namelist_plan"]["previews"]["input"])
        self.assertEqual(self.store.get("task"), before)
        self.assertEqual(self.store.incidents("task"), events)
        self.manager.restart.assert_not_called()

    def test_illegal_layer_edit_and_unchanged_request_are_both_rejected(self):
        before = deepcopy(self.store.get("task"))
        with self.assertRaisesRegex(ValueError, "没有任何修正"):
            self.service.plan(
                "task", self.event["id"], "restart", before["request"]
            )
        invalid = {**before["request"], "history_interval_minutes": 30}
        with self.assertRaisesRegex(ValueError, "垂直|层|eta|p_top"):
            self.service.plan("task", self.event["id"], "restart", invalid)
        self.assertEqual(self.store.get("task"), before)
        self.manager.restart.assert_not_called()
