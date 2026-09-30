import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ReceiptReviewPending
from src.receipts import (
    ITEM_CONFIRMED,
    ITEM_MATCHED,
    ITEM_REF_MISSING,
    ITEM_REJECTED,
    ITEM_VERSION_CHANGED,
)


CREATE_DATA = {'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0, 'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split', 'action_ratio': 2.0}
SPLIT_DELIVERED = 2000

CLERK = Actor("clerk-a", "custody_clerk", "ORG-A")
SUP = Actor("super-a", "custody_supervisor", "ORG-A")
SUP_B = Actor("super-b", "custody_supervisor", "ORG-B")
OFFICER = Actor("officer-a", "settlement_officer", "ORG-A")
TRADER = Actor("trader-a", "trader", "ORG-A")


def receipt(reference, ref_version, result="settled", delivered=1000, cash=12518.0, receipt_no=""):
    return {"receipt_no": receipt_no, "reference": reference, "ref_version": ref_version,
            "result": result, "delivered_quantity": delivered, "cash_paid": cash}


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self._day_seq = iter(range(100, 200))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference="TRD-30001", day=None):
        data = dict(CREATE_DATA)
        data["settlement_day"] = day if day is not None else next(self._day_seq)
        return self.service.create(TRADER, reference, data)

    def test_duplicate_batch_recorded_once_and_retry_idempotent(self):
        record = self._create()
        payload = {"batch_no": "BATCH-1", "items": [receipt(record["reference"], 1, receipt_no="R-1")]}
        first = self.service.submit_receipts(CLERK, payload)
        self.assertTrue(first["created"])
        self.assertEqual(first["status"], "matched")
        self.assertEqual(first["items"][0]["status"], ITEM_MATCHED)

        # 同批次重复回传：不二次落库，返回已存结果
        duplicate = self.service.submit_receipts(CLERK, payload)
        self.assertFalse(duplicate["created"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(duplicate["id"], first["id"])

        # 模拟写入失败后按原批次号重试：已完成结果保留（先复核再重试）
        # 制造一个待复核项，主管确认后重试批次提交，结论必须保留
        record2 = self._create("TRD-30002")
        payload2 = {"batch_no": "BATCH-2", "items": [receipt("TRD-UNKNOWN", 1, receipt_no="R-2")]}
        batch2 = self.service.submit_receipts(CLERK, payload2)
        item = batch2["items"][0]
        self.assertEqual(item["status"], ITEM_REF_MISSING)
        self.service.create(TRADER, "TRD-UNKNOWN", CREATE_DATA)
        self.service.recheck_receipts(SUP, batch2["id"])
        item = self.service.get_receipt_batch(SUP, batch2["id"])["items"][0]
        self.assertEqual(item["status"], ITEM_MATCHED)
        retried = self.service.submit_receipts(CLERK, payload2)
        self.assertFalse(retried["created"])
        self.assertEqual(retried["items"][0]["status"], ITEM_MATCHED)
        self.assertEqual(record2["id"], self.service.get_record(TRADER, record2["id"])["id"])

    def test_missing_reference_becomes_pending_and_recheck_matches(self):
        # 乱序：回执早于本地指令到达
        batch = self.service.submit_receipts(CLERK, {"batch_no": "BATCH-3", "items": [receipt("TRD-LATE", 1)]})
        self.assertEqual(batch["status"], "pending_review")
        self.assertEqual(batch["items"][0]["status"], ITEM_REF_MISSING)

        record = self.service.create(TRADER, "TRD-LATE", CREATE_DATA)
        # 未复核前，审批必须停住
        with self.assertRaises(ReceiptReviewPending):
            self.service.act(OFFICER, record["id"], 1, "approve", {})

        # 重新对账后自动匹配，审批放行
        rechecked = self.service.recheck_receipts(SUP, batch["id"])
        self.assertEqual(rechecked["items"][0]["status"], ITEM_MATCHED)
        approved = self.service.act(OFFICER, record["id"], 1, "approve", {})
        self.assertEqual(approved["state"], "approved")

    def test_version_changed_blocks_approve_and_settle(self):
        record = self._create("TRD-30010")
        # 本地先改动（公司行动调整后 version=2）
        record = self.service.act(Actor("ca", "corporate_actions", "ORG-A"), record["id"], 1, "apply_corporate", {})
        self.assertEqual(record["version"], 2)
        # 回执引用的是旧版本1
        batch = self.service.submit_receipts(CLERK, {"batch_no": "BATCH-4", "items": [
            receipt(record["reference"], 1, delivered=1000, cash=12518.0)]})
        item = batch["items"][0]
        self.assertEqual(item["status"], ITEM_VERSION_CHANGED)
        self.assertFalse(item["version_match"])
        self.assertEqual(item["current_version"], 2)

        with self.assertRaises(ReceiptReviewPending):
            self.service.act(OFFICER, record["id"], 2, "approve", {})

        # 主管确认后放行，审批、交收均可继续
        self.service.decide_receipt(SUP, item["id"], {"decision": "confirmed", "note": "差异已人工核实"})
        record = self.service.act(OFFICER, record["id"], 2, "approve", {})
        settled = self.service.act(OFFICER, record["id"], 3, "settle",
                                   {"delivered_quantity": SPLIT_DELIVERED, "cash_paid": 12518.0})
        self.assertEqual(settled["state"], "settled")

    def test_rejected_receipt_releases_gate_and_terminal_items_stick(self):
        record = self._create("TRD-30020")
        batch = self.service.submit_receipts(CLERK, {"batch_no": "BATCH-5", "items": [receipt("TRD-30020", 9)]})
        item = batch["items"][0]
        self.assertEqual(item["status"], ITEM_VERSION_CHANGED)
        self.service.decide_receipt(SUP, item["id"], {"decision": "rejected", "note": "无效回执"})
        # 驳回后不再拦截审批
        approved = self.service.act(OFFICER, record["id"], 1, "approve", {})
        self.assertEqual(approved["state"], "approved")
        # 终态明细不能重复复核
        with self.assertRaises(Conflict):
            self.service.decide_receipt(SUP, item["id"], {"decision": "confirmed"})

    def test_two_supervisors_concurrent_same_batch_only_one_passes(self):
        record = self._create("TRD-30030")
        payload = {"batch_no": "BATCH-CONC", "items": [receipt("TRD-30030", 1)]}
        results = []
        errors = []

        def submit(actor):
            try:
                results.append(self.service.submit_receipts(actor, payload))
            except Exception as exc:  # pragma: no cover - 并发下不允许出现第二条批次
                errors.append(exc)

        sup2 = Actor("super-a2", "custody_supervisor", "ORG-A")
        t1 = threading.Thread(target=submit, args=(SUP,))
        t2 = threading.Thread(target=submit, args=(sup2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual({r["id"] for r in results}, {results[0]["id"]})
        self.assertEqual(len({r["created"] for r in results}), 2)  # 一个True一个False
        created_flags = sorted(r["created"] for r in results)
        self.assertEqual(created_flags, [False, True])
        batches = self.service.list_receipt_batches(SUP)
        self.assertEqual(len(batches), 1)

    def test_cross_org_clerk_is_denied_and_isolated(self):
        record = self._create("TRD-30040")
        # 专员越权处理其他机构批次：引用其他机构指令按缺失隔离，且审批不受影响
        batch = self.service.submit_receipts(SUP_B, {"batch_no": "BATCH-B1", "items": [receipt("TRD-30040", 1)]})
        self.assertEqual(batch["items"][0]["status"], ITEM_REF_MISSING)
        # ORG-A 的审批不被 ORG-B 的待复核回执拦住
        approved = self.service.act(OFFICER, record["id"], 1, "approve", {})
        self.assertEqual(approved["state"], "approved")

        # 直接操作其他机构批次/明细被拒绝
        own = self.service.submit_receipts(CLERK, {"batch_no": "BATCH-A1", "items": [receipt("TRD-30040", 1)]})
        with self.assertRaises(PermissionDenied):
            self.service.get_receipt_batch(SUP_B, own["id"])
        self.assertEqual([b["id"] for b in self.service.list_receipt_batches(SUP_B)], [batch["id"]])
        self.assertNotIn(own["id"], [b["id"] for b in self.service.list_receipt_batches(SUP_B)])
        item_a = own["items"][0]
        with self.assertRaises(PermissionDenied):
            self.service.decide_receipt(SUP_B, item_a["id"], {"decision": "confirmed"})
        with self.assertRaises(PermissionDenied):
            self.service.recheck_receipts(SUP_B, own["id"])
        # 无权角色
        with self.assertRaises(PermissionDenied):
            self.service.submit_receipts(TRADER, {"batch_no": "X", "items": [receipt("TRD-30040", 1)]})
        with self.assertRaises(PermissionDenied):
            self.service.decide_receipt(CLERK, item_a["id"], {"decision": "confirmed"})

    def test_list_and_audit_show_receipt_version_correspondence(self):
        record = self._create("TRD-30050")
        self.service.submit_receipts(CLERK, {"batch_no": "BATCH-6", "items": [
            receipt("TRD-30050", 1, receipt_no="RC-1")]})

        # 列表中每笔回执与当前版本对应
        items = self.service.list_records(TRADER)
        target = next(item for item in items if item["reference"] == "TRD-30050")
        self.assertEqual(len(target["receipts"]), 1)
        link = target["receipts"][0]
        self.assertEqual(link["ref_version"], 1)
        self.assertEqual(link["current_version"], 1)
        self.assertTrue(link["version_match"])

        # 指令审计时间线包含回执挂接事件
        timeline = self.service.timeline(TRADER, record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("receipt_linked", actions)
        linked = next(event for event in timeline if event["action"] == "receipt_linked")
        self.assertEqual(linked["details"]["ref_version"], 1)
        self.assertEqual(linked["details"]["current_version"], 1)

        # 本地版本推进后，读取时对应关系变为不匹配（回执仍指向旧版本）
        record = self.service.act(Actor("ca", "corporate_actions", "ORG-A"), record["id"], 1, "apply_corporate", {})
        detail = self.service.get_record(TRADER, record["id"])
        link = detail["receipts"][0]
        self.assertEqual(link["current_version"], 2)
        self.assertFalse(link["version_match"])

        # 批次审计时间线记录了接收/重复事件
        audit = self.service.receipt_timeline(SUP, self.service.list_receipt_batches(SUP)[0]["id"])
        self.assertIn("receipt_batch_received", [e["action"] for e in audit])

    def test_intra_batch_duplicate_and_bad_payload_rejected(self):
        record = self._create("TRD-30060")
        with self.assertRaises(Exception):
            self.service.submit_receipts(CLERK, {"batch_no": "BATCH-7", "items": [
                receipt("TRD-30060", 1, receipt_no="DUP"),
                receipt("TRD-30060", 1, receipt_no="DUP")]})
        with self.assertRaises(Exception):
            self.service.submit_receipts(CLERK, {"batch_no": "BATCH-8", "items": []})
        with self.assertRaises(Exception):
            self.service.submit_receipts(CLERK, {"batch_no": "BATCH-9", "items": [
                {"reference": "TRD-30060", "ref_version": 0, "result": "settled",
                 "delivered_quantity": 1, "cash_paid": 1}]})


if __name__ == "__main__":
    unittest.main()
