"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ReceiptReviewPending, ValidationError, text
from .receipts import (
    DECISIONS,
    PENDING_STATES,
    validate_batch,
)
from .repository import Repository
from .rules import DomainRules


# 有待复核回执时必须停住的结算动作
RECEIPT_GATED_ACTIONS = {"approve", "settle"}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _same_org(actor: Actor, organization: str) -> bool:
        """同机构判定。admin 不受机构隔离；任一侧未登记机构时不拦截（旧数据兼容）。"""
        if actor.role == "admin":
            return True
        if not actor.organization or not organization:
            return True
        return actor.organization == organization

    def _ensure_record_org(self, actor: Actor, record: Dict[str, Any]) -> None:
        if actor.role == "admin":
            return
        if not self._same_org(actor, record.get("organization", "")):
            raise PermissionDenied("无权处理其他机构的批次")

    def _attach_receipts(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """为记录列表挂上每笔回执与当前版本的对应关系。"""
        grouped = self.repository.list_receipts_for_records([record["id"] for record in records])
        for record in records:
            items = grouped.get(record["id"], [])
            record["receipts"] = items
            record["receipt_pending"] = any(item["status"] in PENDING_STATES for item in items)
        return records

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, actor.organization)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        records = self._attach_receipts(records)
        if actor.role != "admin" and actor.organization:
            records = [record for record in records if self._same_org(actor, record.get("organization", ""))]
        return records

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._ensure_record_org(actor, record)
        return self._attach_receipts([record])[0]

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self._ensure_record_org(actor, record)
        # 回执对账闸门：引用缺失或版本变化的回执未复核前，审批与交收停住
        if action in RECEIPT_GATED_ACTIONS and self.repository.has_pending_receipt_for_record(record_id):
            raise ReceiptReviewPending("存在待复核的交收回执，处理前审批和交收停住")
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._ensure_record_org(actor, record)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------------
    # 外部存管交收回执对账
    # ------------------------------------------------------------------
    def submit_receipts(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """接收外部存管回传的回执批次。

        同一批次号重复提交（含写入失败后按原批次号重试）只放行一次：
        已落库的批次原样返回，已完成的复核结果保留，不重复记账。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_receipt_action(actor.role, "submit_receipts"):
            raise PermissionDenied("角色无权提交存管回执")
        if actor.role != "admin" and not actor.organization:
            raise PermissionDenied("存管回执必须带机构信息")
        batch = validate_batch(payload or {})
        detail, created = self.repository.save_receipt_batch(
            batch_no=batch["batch_no"],
            organization=actor.organization,
            actor_id=actor.user_id,
            items=batch["items"],
        )
        detail["created"] = created
        detail["deduplicated"] = not created
        return detail

    def list_receipt_batches(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        organization = None if actor.role == "admin" else actor.organization
        return self.repository.list_receipt_batches(organization=organization, limit=limit)

    def get_receipt_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_receipt_batch(batch_id)
        if actor.role != "admin" and actor.organization and batch.get("organization") != actor.organization:
            raise PermissionDenied("无权查看其他机构的回执批次")
        return batch

    def decide_receipt(self, actor: Actor, item_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """存管主管对一笔待复核回执下结论。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_receipt_action(actor.role, "review_receipt"):
            raise PermissionDenied("角色无权复核回执")
        decision = text(payload or {}, "decision")
        if decision not in DECISIONS:
            raise ValidationError("decision只能是confirmed/rejected")
        note = (payload or {}).get("note", "")
        if not isinstance(note, str):
            raise ValidationError("note必须是文本")
        item = self.repository.get_receipt_item(item_id)
        # 越权隔离：专员/主管只能处理本机构批次
        item_batch = self.repository.get_receipt_batch(int(item["batch_id"]))
        if actor.role != "admin" and actor.organization and item_batch.get("organization") != actor.organization:
            raise PermissionDenied("无权处理其他机构的回执批次")
        return self.repository.resolve_receipt_item(item_id, decision, actor.user_id, note.strip())

    def recheck_receipts(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        """重新对账整个批次（迟到指令到达后引用缺失可自动转匹配）。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_receipt_action(actor.role, "review_receipt"):
            raise PermissionDenied("角色无权重新对账回执")
        batch = self.repository.get_receipt_batch(batch_id)
        if actor.role != "admin" and actor.organization and batch.get("organization") != actor.organization:
            raise PermissionDenied("无权处理其他机构的回执批次")
        return self.repository.recheck_receipt_batch(batch_id, actor.user_id)

    def receipt_timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_receipt_batch(batch_id)
        if actor.role != "admin" and actor.organization and batch.get("organization") != actor.organization:
            raise PermissionDenied("无权查看其他机构的回执批次")
        return self.repository.receipt_batch_timeline(batch_id)
