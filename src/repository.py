"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import BatchLocked, BatchCompleted, Conflict, NotFound
from .rules import RECEIPT_INITIAL, RECEIPT_MATCHED, RECEIPT_PENDING, RECEIPT_REJECTED, RECONCILABLE_PENDING_REASONS, ReceiptRules, canonical_payload_hash


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str, receipt_rules: ReceiptRules = None) -> None:
        self.db_path = db_path
        self.receipt_rules = receipt_rules or ReceiptRules()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    org TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    org TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    summary TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    reconciled_by TEXT NOT NULL DEFAULT '',
                    locked_by TEXT NOT NULL DEFAULT '',
                    locked_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES receipt_batches(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL,
                    org TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    reference TEXT NOT NULL,
                    external_id TEXT NOT NULL DEFAULT '',
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    reason TEXT NOT NULL DEFAULT '',
                    record_id INTEGER,
                    local_version INTEGER NOT NULL,
                    matched_version INTEGER,
                    payload_hash TEXT NOT NULL DEFAULT '',
                    matched_payload_hash TEXT NOT NULL DEFAULT '',
                    delivered_quantity INTEGER NOT NULL,
                    cash_paid REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(org, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS receipt_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
                    batch_no TEXT NOT NULL,
                    record_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    org TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_receipts_batch ON receipts(batch_id, seq);
                CREATE INDEX IF NOT EXISTS idx_receipts_ref ON receipts(reference);
                CREATE INDEX IF NOT EXISTS idx_receipts_record ON receipts(record_id);
                CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status);
                CREATE INDEX IF NOT EXISTS idx_receipt_events_receipt ON receipt_events(receipt_id, id);
                CREATE INDEX IF NOT EXISTS idx_receipt_events_record ON receipt_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_attempts_batch ON reconciliation_attempts(batch_no, id);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "org" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN org TEXT NOT NULL DEFAULT ''")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_records_org ON records(org)")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["content"] = json.loads(item["content"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, org: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,org,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), org, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "org": org}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def get_by_reference(self, connection: sqlite3.Connection, reference: str) -> Optional[sqlite3.Row]:
        return connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()

    def list_records(self, state: Optional[str] = None, limit: int = 100, org: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if org:
                if state:
                    rows = connection.execute("SELECT * FROM records WHERE state=? AND org=? ORDER BY id DESC LIMIT ?", (state, org, limit)).fetchall()
                else:
                    rows = connection.execute("SELECT * FROM records WHERE org=? ORDER BY id DESC LIMIT ?", (org, limit)).fetchall()
            elif state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            records = [self._row(row) for row in rows]
            self._attach_receipt_counts(connection, records)
        return records

    @staticmethod
    def _attach_receipt_counts(connection: sqlite3.Connection, records: List[Dict[str, Any]]) -> None:
        if not records:
            return
        by_id = {record["id"]: record for record in records}
        for record in records:
            record["receipt_summary"] = {"total": 0, "matched": 0, "pending_review": 0, "received": 0, "rejected": 0}
        ids = list(by_id)
        id_placeholders = ",".join("?" for _ in ids)
        grouped = connection.execute(
            "SELECT record_id, status, COUNT(*) AS total FROM receipts WHERE record_id IN (%s) GROUP BY record_id, status" % id_placeholders,
            ids,
        ).fetchall()
        for row in grouped:
            summary = by_id[int(row["record_id"])]["receipt_summary"]
            summary[row["status"]] = int(row["total"])
            summary["total"] += int(row["total"])
        # 引用暂缺（record_id尚未回填）的回执按reference并入汇总
        references = list({record["reference"] for record in records})
        if references:
            ref_placeholders = ",".join("?" for _ in references)
            dangling = connection.execute(
                "SELECT reference, status, COUNT(*) AS total FROM receipts WHERE record_id IS NULL AND reference IN (%s) GROUP BY reference, status" % ref_placeholders,
                references,
            ).fetchall()
            id_by_ref = {}
            for record in records:
                id_by_ref.setdefault(record["reference"], record["id"])
            for row in dangling:
                record_id = id_by_ref.get(row["reference"])
                if record_id is None:
                    continue
                summary = by_id[record_id]["receipt_summary"]
                summary[row["status"]] += int(row["total"])
                summary["total"] += int(row["total"])

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def bind_settlement_evidence(self, record_id: int, version: int, actor_id: str) -> None:
        """交收完成后把该指令已匹配回执全部标注到审计事件，保持回执与当前版本可追溯。"""
        now = _now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, batch_no, local_version, matched_version FROM receipts WHERE record_id=? AND status=?",
                (record_id, RECEIPT_MATCHED),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "INSERT INTO receipt_events(receipt_id,batch_no,record_id,event_type,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (int(row["id"]), row["batch_no"], record_id, "settled", actor_id,
                     json.dumps({"record_version": version, "receipt_version": int(row["local_version"]), "matched_version": row["matched_version"]}, ensure_ascii=False, sort_keys=True), now),
                )
            connection.commit()

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )
            connection.commit()

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            item["source"] = "audit"
            result.append(item)
        return result

    def receipt_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM receipt_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            item["source"] = "receipt"
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ------------------------------------------------------------------
    # 回执批次
    # ------------------------------------------------------------------

    def ingest_batch(self, batch_no: str, org: str, actor_id: str, items: List[Dict[str, Any]], fingerprints: List[str]) -> Dict[str, Any]:
        """登记批次：批次重复时整体幂等；同指纹回执（含跨批次重发）只记一次。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch_row = connection.execute("SELECT * FROM receipt_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if batch_row is not None:
                existing = dict(batch_row)
                if existing["org"] != org:
                    connection.rollback()
                    raise Conflict("批次号已被其他机构使用")
                connection.commit()
                return {"batch": self._batch_view(existing), "inserted": 0, "duplicates": len(items), "already_exists": True}
            cursor = connection.execute(
                "INSERT INTO receipt_batches(batch_no,org,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (batch_no, org, "received", actor_id, now, now),
            )
            batch_id = int(cursor.lastrowid)
            inserted = 0
            duplicates = 0
            for item, fingerprint in zip(items, fingerprints):
                inserted_row = connection.execute(
                    "INSERT OR IGNORE INTO receipts(batch_id,batch_no,org,seq,reference,external_id,fingerprint,status,local_version,payload_hash,delivered_quantity,cash_paid,note,content,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, batch_no, org, item["seq"], item["reference"], item["external_id"], fingerprint,
                     RECEIPT_INITIAL, item["local_version"], item["payload_hash"], item["delivered_quantity"],
                     item["cash_paid"], item["note"], json.dumps(item, ensure_ascii=False, sort_keys=True), now, now),
                )
                if inserted_row.rowcount > 0:
                    receipt_id = int(inserted_row.lastrowid)
                    connection.execute(
                        "INSERT INTO receipt_events(receipt_id,batch_no,record_id,event_type,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                        (receipt_id, batch_no, None, "received", actor_id,
                         json.dumps({"seq": item["seq"], "reference": item["reference"], "duplicate": False}, ensure_ascii=False, sort_keys=True), now),
                    )
                    inserted += 1
                else:
                    duplicates += 1
            connection.execute("UPDATE receipt_batches SET updated_at=? WHERE id=?", (now, batch_id))
            connection.commit()
            batch_row = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
        return {"batch": self._batch_view(dict(batch_row)), "inserted": inserted, "duplicates": duplicates, "already_exists": False}

    @staticmethod
    def _batch_view(row: Dict[str, Any]) -> Dict[str, Any]:
        view = dict(row)
        view["summary"] = json.loads(view["summary"]) if view.get("summary") else None
        return view

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM receipt_batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("回执批次不存在")
        return self._batch_view(dict(row))

    def list_batches(self, org: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if org:
                rows = connection.execute("SELECT * FROM receipt_batches WHERE org=? ORDER BY id DESC LIMIT ?", (org, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM receipt_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._batch_view(dict(row)) for row in rows]

    def log_attempt(self, batch_no: str, org: str, actor_id: str, outcome: str, detail: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reconciliation_attempts(batch_no,org,actor_id,outcome,detail,created_at) VALUES(?,?,?,?,?,?)",
                (batch_no, org, actor_id, outcome, detail, _now()),
            )
            connection.commit()

    def list_attempts(self, batch_no: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM reconciliation_attempts WHERE batch_no=? ORDER BY id", (batch_no,)).fetchall()
        return [dict(row) for row in rows]

    def reconcile_batch(self, batch_no: str, org: str, actor_id: str) -> Dict[str, Any]:
        """认领并对账整个批次。单事务，失败整体回滚；调用方负责按原批次号重试。

        已完成批次：仅同一处理人可重放，且只重扫“引用缺失”的回执（乱序补单场景），
        其它已完成结果原样保留；其他处理人提交则拒绝，保证同批次只放行一位。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch_row = connection.execute("SELECT * FROM receipt_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if batch_row is None:
                connection.rollback()
                raise NotFound("回执批次不存在")
            batch = dict(batch_row)
            if batch["org"] != org:
                connection.rollback()
                raise PermissionDeniedLocal("批次属于其他机构，禁止处理")
            if batch["status"] == "reconciled" and batch["reconciled_by"] != actor_id:
                connection.rollback()
                raise BatchCompleted("该批次已由%s完成对账" % batch["reconciled_by"])
            if batch["status"] == "processing" and batch["locked_by"] != actor_id:
                connection.rollback()
                raise BatchLocked("批次正由%s处理，请稍后再试" % batch["locked_by"])
            refresh_only = batch["status"] == "reconciled"
            if not refresh_only:
                connection.execute(
                    "UPDATE receipt_batches SET status='processing',locked_by=?,locked_at=?,updated_at=? WHERE id=?",
                    (actor_id, now, now, batch["id"]),
                )
                rows = connection.execute(
                    "SELECT * FROM receipts WHERE batch_id=? AND (status=? OR (status=? AND reason IN (%s))) ORDER BY seq" % ",".join("?" for _ in RECONCILABLE_PENDING_REASONS),
                    [batch["id"], RECEIPT_INITIAL, RECEIPT_PENDING, *RECONCILABLE_PENDING_REASONS],
                ).fetchall()
            else:
                # 已完成批次重放：只尝试引用此前缺失的回执，其余结果不动。
                rows = connection.execute(
                    "SELECT * FROM receipts WHERE batch_id=? AND status=? AND reason=? ORDER BY seq",
                    (batch["id"], RECEIPT_PENDING, "missing_reference"),
                ).fetchall()
            refreshed = 0
            for receipt_row in rows:
                receipt = self._receipt_row(receipt_row)
                item = dict(receipt["content"])
                item["org"] = receipt["org"]
                record_row = self.get_by_reference(connection, receipt["reference"])
                record = self._row(record_row) if record_row is not None else None
                status, reason, detail = self.receipt_rules.classify(item, record)
                if refresh_only and status == RECEIPT_PENDING and reason == "missing_reference":
                    continue
                record_id = int(record["id"]) if record is not None else None
                matched_version = int(record["version"]) if status == RECEIPT_MATCHED else None
                matched_hash = canonical_payload_hash(record["payload"]) if status == RECEIPT_MATCHED and record is not None else ""
                connection.execute(
                    "UPDATE receipts SET status=?,reason=?,record_id=?,matched_version=?,matched_payload_hash=?,updated_at=? WHERE id=?",
                    (status, reason, record_id, matched_version, matched_hash, now, receipt["id"]),
                )
                detail.update({"seq": receipt["seq"], "from": receipt["status"], "to": status, "refresh": refresh_only})
                connection.execute(
                    "INSERT INTO receipt_events(receipt_id,batch_no,record_id,event_type,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    (receipt["id"], batch_no, record_id, status, actor_id, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
                )
                refreshed += 1
            counts = connection.execute(
                "SELECT status, COUNT(*) AS total FROM receipts WHERE batch_id=? GROUP BY status",
                (batch["id"],),
            ).fetchall()
            by_status = {row["status"]: int(row["total"]) for row in counts}
            reasons = connection.execute(
                "SELECT reason, COUNT(*) AS total FROM receipts WHERE batch_id=? AND status=? GROUP BY reason",
                (batch["id"], RECEIPT_PENDING),
            ).fetchall()
            summary = {
                "total": sum(by_status.values()),
                "matched": by_status.get(RECEIPT_MATCHED, 0),
                "pending_review": by_status.get(RECEIPT_PENDING, 0),
                "received": by_status.get(RECEIPT_INITIAL, 0),
                "rejected": by_status.get(RECEIPT_REJECTED, 0),
                "by_reason": {row["reason"]: int(row["total"]) for row in reasons},
            }
            connection.execute(
                "UPDATE receipt_batches SET status='reconciled',reconciled_by=?,locked_by='',locked_at='',summary=?,updated_at=? WHERE id=?",
                (actor_id, json.dumps(summary, ensure_ascii=False, sort_keys=True), now, batch["id"]),
            )
            result = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch["id"],)).fetchone()
            connection.commit()
        return {"already_completed": refresh_only, "refreshed": refreshed, "batch": self._batch_view(dict(result)), "summary": summary}

    def resolve_review(self, receipt_id: int, org: str, actor_id: str, decision: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("回执不存在")
            receipt = self._receipt_row(row)
            if receipt["org"] != org:
                connection.rollback()
                raise PermissionDeniedLocal("回执属于其他机构，禁止处理")
            if receipt["status"] != RECEIPT_PENDING:
                connection.rollback()
                raise Conflict("回执当前状态不允许复核")
            if decision == "reject":
                status, reason, record_id = RECEIPT_REJECTED, "human_rejected", receipt["record_id"]
                matched_version, matched_hash = None, ""
            else:
                record_row = self.get_by_reference(connection, receipt["reference"])
                if record_row is None:
                    connection.rollback()
                    raise Conflict("引用仍缺失，无法人工匹配")
                record = self._row(record_row)
                item = dict(receipt["content"])
                item["org"] = receipt["org"]
                status, reason, detail = self.receipt_rules.classify(item, record)
                if status != RECEIPT_MATCHED:
                    connection.rollback()
                    raise Conflict("当前版本仍不满足匹配条件：%s" % reason)
                record_id, matched_version, matched_hash = int(record["id"]), int(record["version"]), canonical_payload_hash(record["payload"])
            connection.execute(
                "UPDATE receipts SET status=?,reason=?,record_id=?,matched_version=?,matched_payload_hash=?,updated_at=? WHERE id=?",
                (status, reason, record_id, matched_version, matched_hash, now, receipt_id),
            )
            event_type = "review_matched" if status == RECEIPT_MATCHED else "review_rejected"
            connection.execute(
                "INSERT INTO receipt_events(receipt_id,batch_no,record_id,event_type,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
                (receipt_id, receipt["batch_no"], record_id, event_type, actor_id,
                 json.dumps({"decision": decision, "note": note, "from": RECEIPT_PENDING, "to": status}, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            connection.commit()
        return self._receipt_row(result)

    def list_receipts(self, org: Optional[str] = None, batch_no: Optional[str] = None, reference: Optional[str] = None,
                      status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        if org:
            clauses.append("r.org=?")
            params.append(org)
        if batch_no:
            clauses.append("r.batch_no=?")
            params.append(batch_no)
        if reference:
            clauses.append("r.reference=?")
            params.append(reference)
        if status:
            clauses.append("r.status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT r.* FROM receipts r" + where + " ORDER BY r.id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            receipts = [self._receipt_row(row) for row in rows]
            record_ids = {int(row["record_id"]) for row in rows if row["record_id"] is not None}
            records = {}
            if record_ids:
                placeholders = ",".join("?" for _ in record_ids)
                records = {int(r["id"]): self._row(r) for r in connection.execute(
                    "SELECT * FROM records WHERE id IN (%s)" % placeholders, list(record_ids)
                ).fetchall()}
            dangling_refs = [r["reference"] for r in receipts if r["record_id"] is None]
            ref_records = {}
            if dangling_refs:
                placeholders = ",".join("?" for _ in set(dangling_refs))
                ref_records = {r["reference"]: self._row(r) for r in connection.execute(
                    "SELECT * FROM records WHERE reference IN (%s)" % placeholders, list(set(dangling_refs))
                ).fetchall()}
        for receipt in receipts:
            record = records.get(receipt["record_id"]) if receipt["record_id"] is not None else ref_records.get(receipt["reference"])
            receipt["correspondence"] = self.receipt_rules.correspondence(receipt, record)
        return receipts

    def get_receipt(self, receipt_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                raise NotFound("回执不存在")
            receipt = self._receipt_row(row)
            record = None
            record_row = None
            if receipt["record_id"] is not None:
                record_row = connection.execute("SELECT * FROM records WHERE id=?", (receipt["record_id"],)).fetchone()
            if record_row is None:
                record_row = self.get_by_reference(connection, receipt["reference"])
            if record_row is not None:
                record = self._row(record_row)
        receipt["correspondence"] = self.receipt_rules.correspondence(receipt, record)
        return receipt

    def receipt_blockers(self, record: Dict[str, Any]) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM receipts WHERE record_id=? GROUP BY status",
                (record["id"],),
            ).fetchall()
            counts = {"total": 0, RECEIPT_MATCHED: 0, RECEIPT_PENDING: 0, RECEIPT_INITIAL: 0, RECEIPT_REJECTED: 0}
            for row in rows:
                counts[row["status"]] = int(row["total"])
                counts["total"] += int(row["total"])
            dangling = connection.execute(
                "SELECT status, COUNT(*) AS total FROM receipts WHERE record_id IS NULL AND reference=? GROUP BY status",
                (record["reference"],),
            ).fetchall()
            for row in dangling:
                counts[row["status"]] = counts.get(row["status"], 0) + int(row["total"])
                counts["total"] += int(row["total"])
        return counts

    def batch_audit(self, batch_no: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT e.* FROM receipt_events e JOIN receipt_batches b ON e.batch_no=b.batch_no WHERE e.batch_no=? ORDER BY e.id",
                (batch_no,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


# 仓储内部事务里抛出的跨机构错误，service 层统一映射为 PermissionDenied
class PermissionDeniedLocal(Exception):
    pass
