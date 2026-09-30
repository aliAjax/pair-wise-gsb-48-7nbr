import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split', 'action_ratio': 2.0}
FLOW = [('apply_corporate', 'corporate_actions', {}, 'adjusted'), ('approve', 'settlement_officer', {}, 'approved'), ('settle', 'settlement_officer', {'delivered_quantity': 2000, 'cash_paid': 12518.0}, 'settled')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "TRD-24001", CREATE_DATA)
        self.service.create(Actor("creator", "trader"), "TRD-24001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "trader"), "TRD-24001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "trader"), "TRD-24001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])
