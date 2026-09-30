"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound
from .receipts import (
    ITEM_CONFIRMED,
    ITEM_MATCHED,
    ITEM_REF_MISSING,
    ITEM_VERSION_CHANGED,
    PENDING_STATES,
    batch_status,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
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
                    organization TEXT NOT NULL DEFAULT '',
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
                    organization TEXT NOT NULL DEFAULT '',
                    submitted_by TEXT NOT NULL,
                    status TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES receipt_batches(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    receipt_no TEXT NOT NULL DEFAULT '',
                    reference TEXT NOT NULL,
                    ref_version INTEGER NOT NULL,
                    matched_record_id INTEGER,
                    matched_version INTEGER,
                    current_version INTEGER,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    delivered_quantity REAL NOT NULL,
                    cash_paid REAL NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT,
                    review_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, seq)
                );
                CREATE TABLE IF NOT EXISTS receipt_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES receipt_batches(id) ON DELETE CASCADE,
                    item_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_receipt_items_record ON receipt_items(matched_record_id, status);
                CREATE INDEX IF NOT EXISTS idx_receipt_items_batch ON receipt_items(batch_id, seq);
                CREATE INDEX IF NOT EXISTS idx_receipt_items_ref ON receipt_items(reference);
                CREATE INDEX IF NOT EXISTS idx_receipt_audit_batch ON receipt_audit_events(batch_id, id);
                """
            )
            # 旧库迁移：为结算指令补充机构列（回执越权隔离依赖它）
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "organization" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN organization TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, organization: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,organization,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), organization, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
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

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

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

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # 外部存管交收回执
    # ------------------------------------------------------------------
    @staticmethod
    def _receipt_item_row(connection: sqlite3.Connection, row: sqlite3.Row, batch_org: str = "") -> Dict[str, Any]:
        """补全每笔回执与本地指令当前版本的对应关系（读取时实时关联）。"""
        item = dict(row)
        record = None
        if item["matched_record_id"] is not None:
            record = connection.execute(
                "SELECT id, reference, state, version, organization FROM records WHERE id=?",
                (item["matched_record_id"],),
            ).fetchone()
        else:
            # 引用缺失的回执可能在落库后本地指令才到：按引用实时补关联（同机构）
            record = connection.execute(
                "SELECT id, reference, state, version, organization FROM records WHERE reference=?",
                (item["reference"],),
            ).fetchone()
            if record is not None and batch_org and record["organization"] and record["organization"] != batch_org:
                record = None
        if record is not None:
            item["matched_record_id"] = item["matched_record_id"] if item["matched_record_id"] is not None else int(record["id"])
            item["current_version"] = int(record["version"])
            item["record_state"] = record["state"]
            item["organization"] = record["organization"]
            item["version_match"] = int(item["ref_version"]) == int(record["version"])
        else:
            item["current_version"] = None
            item["record_state"] = None
            item["organization"] = ""
            item["version_match"] = None
        return item

    def _batch_detail(self, connection: sqlite3.Connection, batch_row: sqlite3.Row, include_items: bool = True) -> Dict[str, Any]:
        batch = dict(batch_row)
        rows = connection.execute(
            "SELECT * FROM receipt_items WHERE batch_id=? ORDER BY seq", (batch["id"],)
        ).fetchall()
        items = [self._receipt_item_row(connection, row, batch.get("organization", "")) for row in rows]
        states = [item["status"] for item in items]
        # 批次状态以明细持久状态汇总；读时关联到迟到指令不改变待复核结论，需显式 recheck 才转匹配
        batch["status"] = batch_status(states) if states else batch["status"]
        counts: Dict[str, int] = {}
        for state in states:
            counts[state] = counts.get(state, 0) + 1
        batch["counts"] = counts
        if include_items:
            batch["items"] = items
        else:
            batch.pop("items", None)
        return batch

    @staticmethod
    def _record_receipt_audit(connection: sqlite3.Connection, record_id: Optional[int], action: str,
                              actor_id: str, details: Dict[str, Any], now: str) -> None:
        """把回执对账事件镜像到对应结算指令的审计时间线。"""
        if record_id is None:
            return
        version_row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if version_row is None:
            return
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, int(version_row["version"]),
             json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )

    def save_receipt_batch(
        self,
        batch_no: str,
        organization: str,
        actor_id: str,
        items: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], bool]:
        """幂等落库一个回执批次。

        返回 (批次详情, 是否新建)。批次号已存在（重复回传/写入失败后重试）时
        不覆盖任何已处理结果，原样返回已存批次，且只记录一次明细。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM receipt_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            if existing is not None:
                if organization and existing["organization"] and existing["organization"] != organization:
                    # 批次号是全局命名空间，跨机构撞号时不返回对方批次内容
                    connection.rollback()
                    raise Conflict("批次号已被其他机构使用")
                connection.execute(
                    "INSERT INTO receipt_audit_events(batch_id,item_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                    (int(existing["id"]), None, "receipt_batch_duplicate", actor_id,
                     json.dumps({"batch_no": batch_no, "reason": "批次号已存在，按原批次号返回已存结果"}, ensure_ascii=False, sort_keys=True), now),
                )
                detail = self._batch_detail(connection, existing)
                connection.commit()
                return detail, False

            statuses: List[str] = []
            classified: List[Dict[str, Any]] = []
            for item in items:
                record = connection.execute(
                    "SELECT id, version, organization FROM records WHERE reference=?",
                    (item["reference"],),
                ).fetchone()
                # 跨机构引用按引用缺失处理：不能把别的机构指令作为对账目标
                same_org = record is not None and (not organization or not record["organization"] or record["organization"] == organization)
                if record is None or not same_org:
                    status = ITEM_REF_MISSING
                    record_id = None
                    matched_version = None
                    current_version = None
                elif int(item["ref_version"]) != int(record["version"]):
                    status = ITEM_VERSION_CHANGED
                    record_id = int(record["id"])
                    matched_version = int(item["ref_version"])
                    current_version = int(record["version"])
                else:
                    status = ITEM_MATCHED
                    record_id = int(record["id"])
                    matched_version = int(item["ref_version"])
                    current_version = int(record["version"])
                statuses.append(status)
                classified.append(dict(item, status=status, record_id=record_id,
                                       matched_version=matched_version, current_version=current_version))

            final_status = batch_status(statuses)
            cursor = connection.execute(
                "INSERT INTO receipt_batches(batch_no,organization,submitted_by,status,item_count,created_at) VALUES(?,?,?,?,?,?)",
                (batch_no, organization, actor_id, final_status, len(classified), now),
            )
            batch_id = int(cursor.lastrowid)
            for seq, item in enumerate(classified, start=1):
                item_cursor = connection.execute(
                    "INSERT INTO receipt_items(batch_id,seq,receipt_no,reference,ref_version,matched_record_id,"
                    "matched_version,current_version,status,result,delivered_quantity,cash_paid,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, seq, item["receipt_no"], item["reference"], int(item["ref_version"]),
                     item["record_id"], item["matched_version"], item["current_version"], item["status"],
                     item["result"], float(item["delivered_quantity"]), float(item["cash_paid"]), now),
                )
                self._record_receipt_audit(
                    connection, item["record_id"], "receipt_linked", actor_id,
                    {"batch_no": batch_no, "item_id": int(item_cursor.lastrowid), "receipt_no": item["receipt_no"],
                     "reference": item["reference"], "ref_version": int(item["ref_version"]),
                     "current_version": item["current_version"], "status": item["status"], "result": item["result"]},
                    now,
                )
            counts: Dict[str, int] = {}
            for state in statuses:
                counts[state] = counts[state] + 1 if state in counts else 1
            connection.execute(
                "INSERT INTO receipt_audit_events(batch_id,item_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, None, "receipt_batch_received", actor_id,
                 json.dumps({"batch_no": batch_no, "item_count": len(classified), "counts": counts}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
            detail = self._batch_detail(connection, row)
            connection.commit()
        return detail, True

    def get_receipt_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFound("回执批次不存在")
            return self._batch_detail(connection, row)

    def get_receipt_item(self, item_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM receipt_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFound("回执不存在")
            batch = connection.execute("SELECT organization FROM receipt_batches WHERE id=?", (int(row["batch_id"]),)).fetchone()
            batch_org = batch["organization"] if batch is not None else ""
            return self._receipt_item_row(connection, row, batch_org)

    def list_receipt_batches(self, organization: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if organization:
                rows = connection.execute(
                    "SELECT * FROM receipt_batches WHERE organization=? ORDER BY id DESC LIMIT ?",
                    (organization, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM receipt_batches ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._batch_detail(connection, row, include_items=False) for row in rows]

    def has_pending_receipt_for_record(self, record_id: int) -> bool:
        with self._connect() as connection:
            # 仅统计同机构批次：版本变化按 matched_record_id 挂接；
            # 引用缺失按 reference 关联（乱序早到时 record_id 尚为空）
            row = connection.execute(
                """
                SELECT 1 FROM receipt_items ri
                JOIN receipt_batches rb ON rb.id = ri.batch_id
                JOIN records r ON r.id = ?
                WHERE ri.status IN (?,?)
                  AND (
                        ri.matched_record_id = r.id
                        OR (ri.reference = r.reference
                            AND (r.organization = '' OR rb.organization = '' OR rb.organization = r.organization))
                      )
                LIMIT 1
                """,
                (record_id, ITEM_REF_MISSING, ITEM_VERSION_CHANGED),
            ).fetchone()
        return row is not None

    def list_receipts_for_records(self, record_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        if not record_ids:
            return {}
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as connection:
            records = connection.execute(
                "SELECT id, reference, organization FROM records WHERE id IN (%s)" % placeholders,
                tuple(record_ids),
            ).fetchall()
            ref_by_id = {int(row["id"]): row["reference"] for row in records}
            rows = connection.execute(
                """
                SELECT ri.* FROM receipt_items ri
                LEFT JOIN receipt_batches rb ON rb.id = ri.batch_id
                LEFT JOIN records byref ON byref.reference = ri.reference
                WHERE ri.matched_record_id IN (%s)
                   OR (ri.status = ? AND byref.id IN (%s)
                       AND (byref.organization = '' OR rb.organization = '' OR rb.organization = byref.organization))
                ORDER BY ri.id
                """ % (placeholders, placeholders),
                (*record_ids, ITEM_REF_MISSING, *record_ids),
            ).fetchall()
            grouped: Dict[int, List[Dict[str, Any]]] = {}
            for row in rows:
                batch = connection.execute("SELECT organization FROM receipt_batches WHERE id=?", (int(row["batch_id"]),)).fetchone()
                item = self._receipt_item_row(connection, row, batch["organization"] if batch else "")
                target_id = int(row["matched_record_id"]) if row["matched_record_id"] is not None else None
                if target_id is None:
                    for rid, ref in ref_by_id.items():
                        if ref == row["reference"]:
                            target_id = rid
                            break
                if target_id is not None:
                    grouped.setdefault(target_id, []).append(item)
        return grouped

    def resolve_receipt_item(self, item_id: int, decision: str, actor_id: str, note: str) -> Dict[str, Any]:
        """主管对一笔待复核明细下结论（确认/驳回），原子推进并写批次审计。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM receipt_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("回执不存在")
            if row["status"] not in PENDING_STATES:
                connection.rollback()
                raise Conflict("该回执已处理，无需重复复核")
            connection.execute(
                "UPDATE receipt_items SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                (decision, actor_id, now, note, item_id),
            )
            connection.execute(
                "INSERT INTO receipt_audit_events(batch_id,item_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                (int(row["batch_id"]), item_id, "receipt_reviewed", actor_id,
                 json.dumps({"decision": decision, "previous_status": row["status"], "note": note}, ensure_ascii=False, sort_keys=True), now),
            )
            target_record_id = int(row["matched_record_id"]) if row["matched_record_id"] is not None else None
            if target_record_id is None and decision == ITEM_CONFIRMED:
                linked = connection.execute("SELECT id FROM records WHERE reference=?", (row["reference"],)).fetchone()
                if linked is not None:
                    target_record_id = int(linked["id"])
            self._record_receipt_audit(
                connection, target_record_id, "receipt_reviewed", actor_id,
                {"batch_id": int(row["batch_id"]), "item_id": item_id, "decision": decision,
                 "previous_status": row["status"], "note": note},
                now,
            )
            batch_row = connection.execute(
                "SELECT * FROM receipt_batches WHERE id=?", (int(row["batch_id"]),)
            ).fetchone()
            detail = self._batch_detail(connection, batch_row)
            connection.commit()
        return detail

    def recheck_receipt_batch(self, batch_id: int, actor_id: str) -> Dict[str, Any]:
        """重新对账整个批次：迟到指令已到则自动匹配，版本变化仍需人工。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch_row = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
            if batch_row is None:
                connection.rollback()
                raise NotFound("回执批次不存在")
            rows = connection.execute(
                "SELECT * FROM receipt_items WHERE batch_id=? AND status=? ORDER BY seq",
                (batch_id, ITEM_REF_MISSING),
            ).fetchall()
            moved: List[Dict[str, Any]] = []
            for row in rows:
                record = connection.execute(
                    "SELECT id, version, organization FROM records WHERE reference=?",
                    (row["reference"],),
                ).fetchone()
                same_org = record is not None and (
                    not batch_row["organization"] or not record["organization"] or record["organization"] == batch_row["organization"]
                )
                if record is None or not same_org:
                    continue
                new_status = ITEM_MATCHED if int(row["ref_version"]) == int(record["version"]) else ITEM_VERSION_CHANGED
                connection.execute(
                    "UPDATE receipt_items SET status=?,matched_record_id=?,matched_version=?,current_version=? WHERE id=?",
                    (new_status, int(record["id"]), int(row["ref_version"]), int(record["version"]), int(row["id"])),
                )
                self._record_receipt_audit(
                    connection, int(record["id"]), "receipt_linked", actor_id,
                    {"batch_id": batch_id, "item_id": int(row["id"]), "reference": row["reference"],
                     "ref_version": int(row["ref_version"]), "current_version": int(record["version"]),
                     "status": new_status, "result": row["result"], "via": "recheck"},
                    now,
                )
                moved.append({"item_id": int(row["id"]), "reference": row["reference"], "status": new_status})
            if moved:
                connection.execute(
                    "INSERT INTO receipt_audit_events(batch_id,item_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, None, "receipt_batch_rechecked", actor_id,
                     json.dumps({"matched": moved}, ensure_ascii=False, sort_keys=True), now),
                )
            batch_row = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
            detail = self._batch_detail(connection, batch_row)
            connection.commit()
        return detail

    def receipt_batch_timeline(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFound("回执批次不存在")
            rows = connection.execute(
                "SELECT * FROM receipt_audit_events WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
