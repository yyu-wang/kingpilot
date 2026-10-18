"""写操作溯源账本（M5-T03，方案 §5.5 支柱十七）。

三段式：写前快照 → 执行（由调用方）→ 写后快照 + diff + 证据链 + hash 链。
本模块负责第 3 段落账与查询；快照由调用方注入（M5 无真实写接口）。

约定：
- **append-only**：``WriteLedgerStore`` 只暴露 append / 查询，没有 update/delete
- 失败写入也落账（``status=failed``），``field_diff`` 为空，保留 before “遗照”
- 读取时 ``verify()`` 用 M5-T04 校验 hash 链；断链写 CRITICAL 日志
- SQLite 为同步 API（本地短 IO），异步调用方直接使用
"""

import json
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from config.settings import get_settings
from ledger.hash_chain import (
    GENESIS_HASH,
    ChainVerification,
    compute_record_hash,
    verify_chain,
)
from models.audit import AuditEvent
from models.ledger import (
    FieldDiff,
    ProvenanceChain,
    WriteAction,
    WriteRecord,
    WriteStatus,
)
from observability.audit import AuditLogger


class WriteLedgerStore(Protocol):
    """账本存储接口（append-only）。"""

    def last_record_hash(self) -> str:
        """最后一条记录的 hash；空账本返回 GENESIS_HASH。"""
        ...

    def append(self, record: WriteRecord) -> None:
        """追加一条记录（禁止覆盖/删除）。"""
        ...

    def records(self) -> list[WriteRecord]:
        """按写入顺序返回全部记录（hash 链顺序）。"""
        ...

    def find_by_trace_id(self, trace_id: str) -> list[WriteRecord]:
        """按 trace_id 查询（一次请求的全部写操作）。"""
        ...

    def find_by_business_key(self, business_key: str) -> list[WriteRecord]:
        """按业务单据号查询（审计回放）。"""
        ...


class SqliteWriteLedgerStore:
    """SQLite append-only 实现（单文件、可备份、可审计）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = get_settings().ledger_db_path if db_path is None else db_path
        if isinstance(resolved, Path) and str(resolved) != ":memory:":
            resolved.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(resolved))
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS write_records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    trace_id TEXT NOT NULL,
                    saga_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    form_id TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    kingdee_doc_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    before_snapshot TEXT,
                    after_snapshot TEXT,
                    field_diff TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    prev_record_hash TEXT NOT NULL,
                    record_hash TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_write_records_trace ON write_records (trace_id)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_write_records_business "
                "ON write_records (business_key)"
            )

    def last_record_hash(self) -> str:
        row = self._connection.execute(
            "SELECT record_hash FROM write_records ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return str(row["record_hash"]) if row is not None else GENESIS_HASH

    def append(self, record: WriteRecord) -> None:
        payload = record.model_dump(mode="json")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO write_records (
                    record_id, trace_id, saga_id, timestamp, form_id, business_key,
                    kingdee_doc_id, action, status, before_snapshot, after_snapshot,
                    field_diff, provenance, prev_record_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["record_id"],
                    payload["trace_id"],
                    payload["saga_id"],
                    payload["timestamp"],
                    payload["form_id"],
                    payload["business_key"],
                    payload["kingdee_doc_id"],
                    payload["action"],
                    payload["status"],
                    _dumps_optional(payload["before_snapshot"]),
                    _dumps_optional(payload["after_snapshot"]),
                    json.dumps(payload["field_diff"], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload["provenance"], ensure_ascii=False, sort_keys=True),
                    payload["prev_record_hash"],
                    payload["record_hash"],
                ),
            )

    def records(self) -> list[WriteRecord]:
        rows = self._connection.execute("SELECT * FROM write_records ORDER BY seq ASC").fetchall()
        return [_row_to_record(row) for row in rows]

    def find_by_trace_id(self, trace_id: str) -> list[WriteRecord]:
        rows = self._connection.execute(
            "SELECT * FROM write_records WHERE trace_id = ? ORDER BY seq ASC",
            (trace_id,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def find_by_business_key(self, business_key: str) -> list[WriteRecord]:
        rows = self._connection.execute(
            "SELECT * FROM write_records WHERE business_key = ? ORDER BY seq ASC",
            (business_key,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def close(self) -> None:
        """关闭连接（测试/进程退出用）。"""
        self._connection.close()


class WriteLedger:
    """账本门面：三段式落账 + 查询 + hash 链校验。"""

    def __init__(self, store: WriteLedgerStore, *, audit: AuditLogger | None = None) -> None:
        self._store = store
        self._audit = audit

    def record_write(
        self,
        *,
        trace_id: str,
        form_id: str,
        business_key: str,
        action: WriteAction,
        provenance: ProvenanceChain,
        before_snapshot: Mapping[str, Any] | None = None,
        after_snapshot: Mapping[str, Any] | None = None,
        saga_id: str = "",
        kingdee_doc_id: str = "",
        status: WriteStatus = "success",
        timestamp: datetime | None = None,
        record_id: str | None = None,
    ) -> WriteRecord:
        """落一条写记录：算 diff → 取前条 hash → 算本条 hash → append。"""
        previous_hash = self._store.last_record_hash()
        field_diff = (
            [] if status == "failed" else compute_field_diff(before_snapshot, after_snapshot)
        )
        draft = WriteRecord(
            record_id=record_id or uuid4().hex,
            trace_id=trace_id,
            saga_id=saga_id,
            timestamp=timestamp or datetime.now(),
            form_id=form_id,
            business_key=business_key,
            kingdee_doc_id=kingdee_doc_id,
            action=action,
            status=status,
            before_snapshot=dict(before_snapshot) if before_snapshot is not None else None,
            after_snapshot=dict(after_snapshot) if after_snapshot is not None else None,
            field_diff=field_diff,
            provenance=provenance,
            prev_record_hash=previous_hash,
            record_hash="",
        )
        digest = compute_record_hash(draft.to_hash_payload(), previous_hash)
        record = draft.model_copy(update={"record_hash": digest})
        self._store.append(record)

        if self._audit is not None:
            self._audit.emit(
                AuditEvent(
                    event_type="ledger.write",
                    trace_id=trace_id,
                    user_id=provenance.user_id,
                    action=action,
                    form_id=form_id,
                    business_key=business_key,
                    risk_score=provenance.risk_score,
                    risk_level=provenance.risk_level,
                    approval_chain=list(provenance.approval_chain),
                    status="success" if status == "success" else "failed",
                    saga_id=saga_id,
                    detail={
                        "record_id": record.record_id,
                        "record_hash": record.record_hash,
                        "kingdee_doc_id": kingdee_doc_id,
                    },
                )
            )
        return record

    def verify(self) -> ChainVerification:
        """校验全链完整性（读取时调用）。"""
        return verify_chain([record.to_hash_payload() for record in self._store.records()])

    def records(self) -> list[WriteRecord]:
        return self._store.records()

    def find_by_trace_id(self, trace_id: str) -> list[WriteRecord]:
        return self._store.find_by_trace_id(trace_id)

    def find_by_business_key(self, business_key: str) -> list[WriteRecord]:
        return self._store.find_by_business_key(business_key)


def compute_field_diff(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> list[FieldDiff]:
    """字段级 diff：递归 dict、点号路径、列表整体比较。"""
    before_flat = _flatten(before)
    after_flat = _flatten(after)
    diffs: list[FieldDiff] = []
    for field_name in sorted(set(before_flat) | set(after_flat)):
        old_value = before_flat.get(field_name)
        new_value = after_flat.get(field_name)
        if old_value != new_value:
            diffs.append(
                FieldDiff(
                    field_name=field_name,
                    old_value=old_value,
                    new_value=new_value,
                )
            )
    return diffs


def _flatten(value: Mapping[str, Any] | None, prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict 压平为点号路径；标量/列表按整值处理。"""
    if value is None:
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        field_name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping):
            result.update(_flatten(item, field_name))
        else:
            result[field_name] = item
    return result


def _dumps_optional(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _row_to_record(row: sqlite3.Row) -> WriteRecord:
    return WriteRecord.model_validate(
        {
            "record_id": row["record_id"],
            "trace_id": row["trace_id"],
            "saga_id": row["saga_id"],
            "timestamp": row["timestamp"],
            "form_id": row["form_id"],
            "business_key": row["business_key"],
            "kingdee_doc_id": row["kingdee_doc_id"],
            "action": row["action"],
            "status": row["status"],
            "before_snapshot": _loads_optional(row["before_snapshot"]),
            "after_snapshot": _loads_optional(row["after_snapshot"]),
            "field_diff": json.loads(row["field_diff"]),
            "provenance": json.loads(row["provenance"]),
            "prev_record_hash": row["prev_record_hash"],
            "record_hash": row["record_hash"],
        }
    )


def _loads_optional(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)
