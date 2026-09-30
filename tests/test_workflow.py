import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split', 'action_ratio': 2.0}
FLOW = [('apply_corporate', 'corporate_actions', {}, 'adjusted'), ('approve', 'settlement_officer', {}, 'approved'), ('settle', 'settlement_officer', {'delivered_quantity': 2000, 'cash_paid': 12518.0}, 'settled')]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-24001", CREATE_DATA)
        self.assertEqual(record["state"], "captured")
        for action, role, data, expected_state in FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "trader"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 1)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])
