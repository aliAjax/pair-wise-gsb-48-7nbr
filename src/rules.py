"""证券结算与企业行动处理领域规则与状态转换。"""
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {'apply_corporate': {'corporate_actions'}, 'approve': {'settlement_officer'}, 'settle': {'settlement_officer'}, 'fail': {'settlement_officer'}, 'reverse': {'corporate_actions', 'settlement_officer'}}
TRANSITIONS = {'apply_corporate': {'captured': 'adjusted'}, 'approve': {'captured': 'approved', 'adjusted': 'approved'}, 'settle': {'approved': 'settled'}, 'fail': {'approved': 'failed'}, 'reverse': {'settled': 'reversed', 'failed': 'reversed'}}

# 回执生命周期：received（已登记）/ matched（已匹配）/ pending_review（待复核）/ rejected（人工驳回）
RECEIPT_INITIAL = "received"
RECEIPT_MATCHED = "matched"
RECEIPT_PENDING = "pending_review"
RECEIPT_REJECTED = "rejected"
RECEIPT_STATUSES = {RECEIPT_INITIAL, RECEIPT_MATCHED, RECEIPT_PENDING, RECEIPT_REJECTED}
# 重新对账时只有“未处理”和“引用暂缺”的回执会再次尝试匹配；
# 版本变化/数量不符等必须人工复核，系统不得自行翻盘。
RECONCILABLE_PENDING_REASONS = {"missing_reference"}

RECEIPT_INGEST_ROLES = {'custody_clerk'}
RECEIPT_RECONCILE_ROLES = {'settlement_officer'}
RECEIPT_REVIEW_ROLES = {'settlement_officer'}
REVIEW_DECISIONS = {"match", "reject"}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(RECEIPT_INGEST_ROLES)
        all_roles.update(RECEIPT_RECONCILE_ROLES)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_ingest(self, role: str) -> bool:
        return role == "admin" or role in RECEIPT_INGEST_ROLES

    def role_can_reconcile(self, role: str) -> bool:
        return role == "admin" or role in RECEIPT_RECONCILE_ROLES

    def role_can_review(self, role: str) -> bool:
        return role == "admin" or role in RECEIPT_REVIEW_ROLES

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", ["CNY", "USD", "HKD"])
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)


def canonical_payload_hash(payload: Dict[str, Any]) -> str:
    """本地指令当前版本的规范化哈希，供回执版本比对。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def receipt_fingerprint(org: str, item: Dict[str, Any]) -> str:
    """回执业务指纹：外部存管重发（哪怕换批次号）也视为同一张。"""
    parts = [org, item["reference"], item.get("external_id", ""), int(item["local_version"]), int(item["delivered_quantity"]), float(item["cash_paid"])]
    body = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ReceiptRules:
    """回执登记、对账分类与结算闸门规则。"""

    INITIAL_STATUS = RECEIPT_INITIAL
    MATCHED = RECEIPT_MATCHED
    PENDING = RECEIPT_PENDING
    REJECTED = RECEIPT_REJECTED

    def validate_item(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("回执条目必须是对象")
        item: Dict[str, Any] = {}
        item["seq"] = integer(raw, "seq", 0)
        item["reference"] = text(raw, "reference")
        item["local_version"] = integer(raw, "local_version", 1)
        item["delivered_quantity"] = integer(raw, "delivered_quantity", 0)
        item["cash_paid"] = number(raw, "cash_paid", 0)
        if "external_id" in raw and raw["external_id"] is not None:
            item["external_id"] = text(raw, "external_id")
        else:
            item["external_id"] = ""
        echoed = raw.get("payload_hash")
        if echoed is not None and not isinstance(echoed, str):
            raise ValidationError("payload_hash必须是文本")
        item["payload_hash"] = (echoed or "").strip()
        item["note"] = raw.get("note", "") if isinstance(raw.get("note", ""), str) else ""
        return item

    def validate_items(self, raw: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("receipts至少需要一条回执")
        if len(raw) > 2000:
            raise ValidationError("单批次回执不能超过2000条")
        items = [self.validate_item(entry) for entry in raw]
        seen_seq = set()
        for item in items:
            if item["seq"] in seen_seq:
                raise ValidationError("批次内回执序号%s重复" % item["seq"])
            seen_seq.add(item["seq"])
        return items

    def classify(self, item: Dict[str, Any], record: Optional[Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
        """返回(回执状态, 原因, 比对明细)。乱序到达不影响判定。"""
        detail: Dict[str, Any] = {"reference": item["reference"], "receipt_version": int(item["local_version"])}
        if record is None:
            return self.PENDING, "missing_reference", detail
        payload = record["payload"]
        detail.update({"record_id": int(record["id"]), "current_version": int(record["version"]), "record_state": record["state"]})
        if record.get("org") and item.get("org") and record["org"] != item["org"]:
            return self.PENDING, "org_mismatch", detail
        if int(item["local_version"]) != int(record["version"]):
            detail["version_matches"] = False
            return self.PENDING, "version_changed", detail
        detail["version_matches"] = True
        if item["payload_hash"]:
            current_hash = canonical_payload_hash(payload)
            detail["receipt_payload_hash"] = item["payload_hash"]
            detail["current_payload_hash"] = current_hash
            if item["payload_hash"] != current_hash:
                return self.PENDING, "payload_changed", detail
        required_quantity = int(payload.get("effective_quantity", payload["quantity"]))
        detail["required_quantity"] = required_quantity
        detail["delivered_quantity"] = int(item["delivered_quantity"])
        if int(item["delivered_quantity"]) != required_quantity:
            return self.PENDING, "quantity_mismatch", detail
        detail["required_cash"] = float(payload["net_amount"])
        detail["cash_paid"] = float(item["cash_paid"])
        if float(item["cash_paid"]) < float(payload["net_amount"]):
            return self.PENDING, "cash_shortfall", detail
        return self.MATCHED, "", detail

    def guard(self, action: str, pending_count: int, matched_count: int, receipt_count: int) -> Optional[str]:
        """审批/交收闸门：有待复核一律停住；交收在已有回执时必须存在匹配回执。"""
        if pending_count > 0:
            return "存在%s笔待复核回执，请先处理" % pending_count
        if action == "settle" and receipt_count > 0 and matched_count == 0:
            return "回执尚未对账匹配，交收停住"
        return None

    def correspondence(self, receipt: Dict[str, Any], record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """组装“回执版本 ↔ 指令当前版本”对应关系，供列表与审计展示。"""
        view = {
            "receipt_id": receipt["id"],
            "batch_no": receipt["batch_no"],
            "seq": receipt["seq"],
            "status": receipt["status"],
            "reason": receipt.get("reason", ""),
            "reference": receipt["reference"],
            "receipt_version": receipt["local_version"],
        }
        if record is None:
            view.update({"record_id": receipt.get("record_id"), "current_version": None, "version_matches": None, "record_state": None, "current_payload_hash": None})
            return view
        current_hash = canonical_payload_hash(record["payload"])
        view.update({
            "record_id": int(record["id"]),
            "current_version": int(record["version"]),
            "version_matches": int(receipt["local_version"]) == int(record["version"]),
            "record_state": record["state"],
            "receipt_payload_hash": receipt.get("payload_hash", ""),
            "current_payload_hash": current_hash,
            "payload_matches": (not receipt.get("payload_hash")) or receipt["payload_hash"] == current_hash,
        })
        return view
