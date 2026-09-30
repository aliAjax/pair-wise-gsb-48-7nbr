"""业务用例编排、权限检查与审计。"""
import sqlite3
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, RetryExhausted, ValidationError, text
from .repository import PermissionDeniedLocal, Repository
from .rules import REVIEW_DECISIONS, DomainRules, ReceiptRules, receipt_fingerprint


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, receipt_rules: ReceiptRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.receipt_rules = receipt_rules or ReceiptRules()
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
    def _org(actor: Actor) -> str:
        organization = actor.organization.strip()
        if not organization:
            raise PermissionDenied("回执业务必须提供机构标识X-Org")
        return organization

    def _same_org(self, actor: Actor, org: str) -> None:
        if actor.role == "admin":
            return
        if actor.organization.strip() and org and actor.organization.strip() != org:
            raise PermissionDenied("无权处理其他机构的数据")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, actor.organization.strip())

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        org = None if actor.role == "admin" else actor.organization.strip()
        return self.repository.list_records(state=state, limit=limit, org=org)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._same_org(actor, record.get("org", ""))
        record["receipt_summary"] = self.repository.receipt_blockers(record)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self._same_org(actor, record.get("org", ""))
        self.rules.require_transition(record, action)
        if action in {"approve", "settle"}:
            counts = self.repository.receipt_blockers(record)
            message = self.receipt_rules.guard(action, counts[self.receipt_rules.PENDING], counts[self.receipt_rules.MATCHED], counts["total"])
            if message:
                raise Conflict(message)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if action == "settle":
            self.repository.bind_settlement_evidence(record_id, result["version"], actor.user_id)
        return result

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        self._same_org(actor, record.get("org", ""))
        events = self.audit.timeline(record_id) + self.repository.receipt_timeline(record_id)
        events.sort(key=lambda item: (item["created_at"], item["id"]))
        return events

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------------
    # 外部存管回执
    # ------------------------------------------------------------------

    def ingest_receipts(self, actor: Actor, batch_no: str, raw_items: Any) -> Dict[str, Any]:
        """登记外部存管回传批次：重复批次/重复回执幂等，只记一次。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_ingest(actor.role):
            raise PermissionDenied("角色无权登记回执")
        org = self._org(actor)
        batch_no = text({"batch_no": batch_no}, "batch_no")
        items = self.receipt_rules.validate_items(raw_items)
        fingerprints = [receipt_fingerprint(org, item) for item in items]
        result = self.repository.ingest_batch(batch_no, org, actor.user_id, items, fingerprints)
        return result

    def reconcile_batch(self, actor: Actor, batch_no: str, max_attempts: int = 3) -> Dict[str, Any]:
        """主管对账：批次认领互斥；写入失败按原批次号重试，已完成结果原样保留。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_reconcile(actor.role):
            raise PermissionDenied("角色无权对账回执批次")
        org = self._org(actor)
        batch_no = text({"batch_no": batch_no}, "batch_no")
        # 先做机构越权检查（不抢锁），越权直接拒绝。
        batch = self.repository.get_batch(batch_no)
        if batch["org"] != org:
            raise PermissionDenied("批次属于其他机构，禁止处理")

        attempts = 0
        last_error: Optional[Exception] = None
        while attempts < max_attempts:
            attempts += 1
            try:
                result = self.repository.reconcile_batch(batch_no, org, actor.user_id)
                self.repository.log_attempt(batch_no, org, actor.user_id, "succeeded", "attempt=%s" % attempts)
                result["attempts"] = attempts
                return result
            except Conflict:
                raise
            except PermissionDeniedLocal as exc:
                raise PermissionDenied(str(exc)) from exc
            except sqlite3.Error as exc:
                # 写入失败：整批回滚后按原批次号重试，已完成结果由唯一约束与批次状态保留。
                last_error = exc
                self.repository.log_attempt(batch_no, org, actor.user_id, "retry", "attempt=%s error=%s" % (attempts, exc))
        self.repository.log_attempt(batch_no, org, actor.user_id, "failed", str(last_error))
        raise RetryExhausted("批次%s对账重试%s次后仍失败" % (batch_no, max_attempts))

    def resolve_review(self, actor: Actor, receipt_id: int, decision: str, note: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review(actor.role):
            raise PermissionDenied("角色无权复核回执")
        org = self._org(actor)
        decision = text({"decision": decision}, "decision")
        if decision not in REVIEW_DECISIONS:
            raise ValidationError("decision只能是match/reject")
        note = note if isinstance(note, str) else ""
        try:
            return self.repository.resolve_review(receipt_id, org, actor.user_id, decision, note.strip())
        except PermissionDeniedLocal as exc:
            raise PermissionDenied(str(exc)) from exc

    def list_receipts(self, actor: Actor, batch_no: Optional[str] = None, reference: Optional[str] = None,
                      status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        org = None if actor.role == "admin" else actor.organization.strip()
        if actor.role != "admin" and not org:
            raise PermissionDenied("回执查询必须提供机构标识X-Org")
        return self.repository.list_receipts(org=org, batch_no=batch_no, reference=reference, status=status, limit=limit)

    def get_receipt(self, actor: Actor, receipt_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        receipt = self.repository.get_receipt(receipt_id)
        self._same_org(actor, receipt.get("org", ""))
        return receipt

    def list_batches(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        org = None if actor.role == "admin" else actor.organization.strip()
        if actor.role != "admin" and not org:
            raise PermissionDenied("批次查询必须提供机构标识X-Org")
        return self.repository.list_batches(org=org, limit=limit)

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_no)
        self._same_org(actor, batch.get("org", ""))
        return batch

    def batch_audit(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_no)
        self._same_org(actor, batch.get("org", ""))
        return {
            "batch": batch,
            "events": self.repository.batch_audit(batch_no),
            "attempts": self.repository.list_attempts(batch_no),
        }
