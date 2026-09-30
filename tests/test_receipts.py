import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, BatchCompleted, Conflict, PermissionDenied, RetryExhausted, ValidationError
from src.rules import canonical_payload_hash


def data(**overrides):
    base = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0,
            'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'none', 'action_ratio': 1.0}
    base.update(overrides)
    return base


class ReceiptTestBase(unittest.TestCase):
    org_a = "ORG-A"
    org_b = "ORG-B"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.trader = Actor("trader-1", "trader", self.org_a)
        self.clerk = Actor("clerk-1", "custody_clerk", self.org_a)
        self.clerk_b = Actor("clerk-2", "custody_clerk", self.org_b)
        self.manager = Actor("mgr-1", "settlement_officer", self.org_a)
        self.manager2 = Actor("mgr-2", "settlement_officer", self.org_a)
        self.manager_b = Actor("mgr-3", "settlement_officer", self.org_b)

    def tearDown(self):
        self.temp.cleanup()

    def create_instruction(self, reference="TRD-1", actor=None, **overrides):
        actor = actor or self.trader
        return self.service.create(actor, reference, data(**overrides))

    def matched_item(self, record, seq=1, **overrides):
        item = {"seq": seq, "reference": record["reference"], "local_version": record["version"],
                "delivered_quantity": record["payload"]["quantity"], "cash_paid": record["payload"]["net_amount"],
                "payload_hash": canonical_payload_hash(record["payload"])}
        item.update(overrides)
        return item

    def reconcile(self, actor=None, batch_no="BATCH-1"):
        return self.service.reconcile_batch(actor or self.manager, batch_no)


class DuplicateAndOrderTest(ReceiptTestBase):
    def test_duplicate_batch_and_repeated_receipts_recorded_once(self):
        record = self.create_instruction()
        items = [self.matched_item(record, seq=1)]
        first = self.service.ingest_receipts(self.clerk, "BATCH-1", items)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(first["duplicates"], 0)
        # 整批重发、乱序到达
        repeat = self.service.ingest_receipts(self.clerk, "BATCH-1", list(reversed(items)))
        self.assertTrue(repeat["already_exists"])
        self.assertEqual(repeat["inserted"], 0)
        self.assertEqual(repeat["duplicates"], 1)
        # 同一回执换批次号重发（跨批次重投）仍只记一次
        cross = self.service.ingest_receipts(self.clerk, "BATCH-2", items)
        self.assertEqual(cross["inserted"], 0)
        self.assertEqual(cross["duplicates"], 1)

        result = self.reconcile()
        self.assertEqual(result["summary"]["matched"], 1)
        receipts = self.service.list_receipts(self.manager, batch_no="BATCH-1")
        self.assertEqual(len(receipts), 1)

    def test_duplicate_seq_within_batch_rejected(self):
        record = self.create_instruction()
        items = [self.matched_item(record, seq=1), self.matched_item(record, seq=1, cash_paid=1.0)]
        with self.assertRaises(ValidationError):
            self.service.ingest_receipts(self.clerk, "BATCH-1", items)

    def test_out_of_order_arrival_does_not_affect_matching(self):
        record = self.create_instruction()
        items = [
            self.matched_item(record, seq=2, cash_paid=13000.0),
            self.matched_item(record, seq=1),
        ]
        self.service.ingest_receipts(self.clerk, "BATCH-1", items)
        result = self.reconcile()
        # seq=2 资金多付视为正常（以回执序号排序处理，乱序不影响判定）
        self.assertEqual(result["summary"]["matched"], 2)


class ReconcileClassificationTest(ReceiptTestBase):
    def test_missing_reference_version_change_and_mismatch_go_pending(self):
        record = self.create_instruction()
        record2 = self.create_instruction("TRD-2", price=13.0)
        items = [
            self.matched_item(record, seq=1),
            {"seq": 2, "reference": "TRD-MISSING", "local_version": 1, "delivered_quantity": 1, "cash_paid": 1.0},
            self.matched_item(record2, seq=3, local_version=record2["version"] + 1),
            self.matched_item(record, seq=4, delivered_quantity=999),
            self.matched_item(record, seq=5, payload_hash="deadbeef", external_id="E5"),
        ]
        self.service.ingest_receipts(self.clerk, "BATCH-1", items)
        summary = self.reconcile()["summary"]
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["pending_review"], 4)
        reasons = summary["by_reason"]
        self.assertEqual(reasons["missing_reference"], 1)
        self.assertEqual(reasons["version_changed"], 1)
        self.assertEqual(reasons["quantity_mismatch"], 1)
        self.assertEqual(reasons["payload_changed"], 1)

    def test_missing_reference_picked_up_after_instruction_arrives(self):
        items = [{"seq": 1, "reference": "TRD-LATE", "local_version": 1,
                  "delivered_quantity": 1000, "cash_paid": 12518.0}]
        self.service.ingest_receipts(self.clerk, "BATCH-1", items)
        summary = self.reconcile()["summary"]
        self.assertEqual(summary["pending_review"], 1)
        record = self.create_instruction("TRD-LATE")
        replay = self.reconcile()
        self.assertTrue(replay["already_completed"])
        self.assertEqual(replay["summary"]["matched"], 1)
        self.assertEqual(replay["summary"]["pending_review"], 0)
        receipts = self.service.list_receipts(self.manager, batch_no="BATCH-1")
        correspondence = receipts[0]["correspondence"]
        self.assertTrue(correspondence["version_matches"])
        self.assertEqual(correspondence["current_version"], record["version"])


class GateTest(ReceiptTestBase):
    def _approved(self, record):
        return self.service.act(self.manager, record["id"], record["version"], "approve", {})

    def test_approve_and_settle_blocked_while_pending_review(self):
        record = self.create_instruction()
        items = [
            self.matched_item(record, seq=1),
            self.matched_item(record, seq=2, delivered_quantity=1),
        ]
        self.service.ingest_receipts(self.clerk, "BATCH-1", items)
        self.reconcile()
        with self.assertRaises(Conflict):
            self._approved(record)
        # 清理待复核（驳回异常回执）后审批与交收放行
        pending = [r for r in self.service.list_receipts(self.manager) if r["status"] == "pending_review"]
        self.service.resolve_review(self.manager, pending[0]["id"], "reject", "数量不符")
        record = self.service.get_record(self.manager, record["id"])
        record = self._approved(record)
        settled = self.service.act(self.manager, record["id"], record["version"], "settle",
                                   {"delivered_quantity": 1000, "cash_paid": 12518.0})
        self.assertEqual(settled["state"], "settled")

    def test_settle_requires_matched_receipt_when_receipts_exist(self):
        record = self.create_instruction()
        # 只有一张 received 状态（批次登记但未对账）的回执
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])
        record = self._approved(record)
        with self.assertRaises(Conflict):
            self.service.act(self.manager, record["id"], record["version"], "settle",
                             {"delivered_quantity": 1000, "cash_paid": 12518.0})

    def test_manual_match_after_revision(self):
        record = self.create_instruction()
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record, local_version=99)])
        self.reconcile()
        pending = [r for r in self.service.list_receipts(self.manager) if r["status"] == "pending_review"]
        self.assertEqual(pending[0]["reason"], "version_changed")
        # 当前版本仍不一致，不能强行匹配
        with self.assertRaises(Conflict):
            self.service.resolve_review(self.manager, pending[0]["id"], "match", "再确认")


class ConcurrencyAndRetryTest(ReceiptTestBase):
    def test_two_managers_submitting_same_batch_only_one_passes(self):
        record = self.create_instruction()
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])
        barrier = threading.Barrier(2)
        outcomes = {}

        def submit(actor, key):
            barrier.wait()
            try:
                self.service.reconcile_batch(actor, "BATCH-1")
                outcomes[key] = "ok"
            except BatchCompleted:
                outcomes[key] = "blocked"
            except Conflict:
                outcomes[key] = "blocked"

        t1 = threading.Thread(target=submit, args=(self.manager, "a"))
        t2 = threading.Thread(target=submit, args=(self.manager2, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes.values()), ["blocked", "ok"])

    def test_write_failure_retries_with_same_batch_no_and_keeps_result(self):
        record = self.create_instruction()
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])
        real = self.service.repository.reconcile_batch
        state = {"calls": 0}

        def flaky(batch_no, org, actor_id):
            state["calls"] += 1
            if state["calls"] == 1:
                raise sqlite3.OperationalError("disk I/O error")
            return real(batch_no, org, actor_id)

        self.service.repository.reconcile_batch = flaky
        result = self.service.reconcile_batch(self.manager, "BATCH-1")
        self.assertEqual(state["calls"], 2)
        self.assertEqual(result["attempts"], 2)
        attempts = self.service.batch_audit(self.manager, "BATCH-1")["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["retry", "succeeded"])
        # 再次重放不产生重复结果
        before = len(self.service.batch_audit(self.manager, "BATCH-1")["events"])
        self.service.reconcile_batch(self.manager, "BATCH-1")
        after = len(self.service.batch_audit(self.manager, "BATCH-1")["events"])
        self.assertEqual(before, after)

    def test_retry_exhaustion_raises(self):
        record = self.create_instruction()
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])

        def always_fail(*args, **kwargs):
            raise sqlite3.OperationalError("disk full")

        self.service.repository.reconcile_batch = always_fail
        with self.assertRaises(RetryExhausted):
            self.service.reconcile_batch(self.manager, "BATCH-1", max_attempts=2)


class PermissionTest(ReceiptTestBase):
    def test_clerk_cannot_reconcile_or_review(self):
        record = self.create_instruction()
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])
        with self.assertRaises(PermissionDenied):
            self.service.reconcile_batch(self.clerk, "BATCH-1")
        self.reconcile()
        with self.assertRaises(PermissionDenied):
            self.service.resolve_review(self.clerk, 1, "reject")

    def test_cross_org_batch_rejected(self):
        record = self.create_instruction()
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])
        # 其他机构专员不能复用该批次号
        with self.assertRaises(Conflict):
            self.service.ingest_receipts(self.clerk_b, "BATCH-1", [self.matched_item(record)])
        # 其他机构主管不能对账/查看该批次
        with self.assertRaises(PermissionDenied):
            self.service.reconcile_batch(self.manager_b, "BATCH-1")
        with self.assertRaises(PermissionDenied):
            self.service.get_batch(self.manager_b, "BATCH-1")
        # 机构数据隔离：专员看不到其他机构回执
        self.assertEqual(self.service.list_receipts(self.clerk_b), [])

    def test_cross_org_receipt_routed_to_review_not_auto_matched(self):
        record_b = self.create_instruction("TRD-B", actor=Actor("tb", "trader", self.org_b))
        # A机构批次里出现引用B机构指令的回执
        item = {"seq": 1, "reference": "TRD-B", "local_version": record_b["version"],
                "delivered_quantity": 1000, "cash_paid": 12518.0,
                "payload_hash": canonical_payload_hash(record_b["payload"])}
        self.service.ingest_receipts(self.clerk, "BATCH-1", [item])
        summary = self.reconcile()["summary"]
        self.assertEqual(summary["pending_review"], 1)
        self.assertEqual(summary["by_reason"]["org_mismatch"], 1)


class VisibilityTest(ReceiptTestBase):
    def test_list_and_audit_show_receipt_version_correspondence(self):
        record = self.create_instruction()
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])
        self.reconcile()
        receipt = self.service.list_receipts(self.manager, batch_no="BATCH-1")[0]
        view = receipt["correspondence"]
        self.assertTrue(view["version_matches"])
        self.assertTrue(view["payload_matches"])
        self.assertEqual(view["receipt_version"], record["version"])
        self.assertEqual(view["current_version"], record["version"])
        # 记录详情带回执汇总，记录审计时间线包含回执事件
        detail = self.service.get_record(self.manager, record["id"])
        self.assertEqual(detail["receipt_summary"]["matched"], 1)
        timeline = self.service.timeline(self.manager, record["id"])
        receipt_events = [event for event in timeline if event["source"] == "receipt"]
        self.assertTrue(any(event["event_type"] == "matched" for event in receipt_events))
        # 批次审计可见每笔处理事件与尝试记录
        audit = self.service.batch_audit(self.manager, "BATCH-1")
        self.assertEqual(len(audit["events"]), 2)  # received + matched
        self.assertTrue(audit["attempts"])

    def test_correspondence_updates_after_local_version_changes(self):
        # 指令带拆股，回执按 v1 匹配后本地又应用公司行动到 v2：对应关系应显示已脱节
        record = self.create_instruction(corporate_action="split", action_ratio=2.0)
        self.service.ingest_receipts(self.clerk, "BATCH-1", [self.matched_item(record)])
        self.reconcile()
        receipt = self.service.list_receipts(self.manager)[0]
        self.assertTrue(receipt["correspondence"]["version_matches"])
        record = self.service.act(Actor("ca", "corporate_actions", self.org_a), record["id"],
                                  record["version"], "apply_corporate", {})
        self.assertEqual(record["version"], 2)
        receipt = self.service.list_receipts(self.manager)[0]
        view = receipt["correspondence"]
        self.assertFalse(view["version_matches"])
        self.assertEqual(view["receipt_version"], 1)
        self.assertEqual(view["current_version"], 2)
        self.assertFalse(view["payload_matches"])


if __name__ == "__main__":
    unittest.main()
