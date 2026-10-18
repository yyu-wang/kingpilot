"""M5-T03：三段式账本单测。

覆盖字段 diff（顶层/嵌套/新建/删除）、失败写入、hash 链、SQLite 落盘重读、
审计查询、篡改检测与 append-only 接口约束。
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ledger.hash_chain import GENESIS_HASH
from ledger.write_ledger import (
    SqliteWriteLedgerStore,
    WriteLedger,
    compute_field_diff,
)
from models.ledger import ProvenanceChain, WriteRecord


def _provenance() -> ProvenanceChain:
    return ProvenanceChain(
        user_original_input="把金额改成 8 万",
        user_id="u001",
        session_id="s1",
        llm_reasoning="修改 SO1 金额",
        risk_score=42,
        risk_level="confirm",
    )


def _ledger(tmp_path: Path) -> tuple[WriteLedger, SqliteWriteLedgerStore]:
    store = SqliteWriteLedgerStore(tmp_path / "ledger.db")
    return WriteLedger(store), store


def _record(
    ledger: WriteLedger,
    *,
    trace_id: str = "trace-1",
    business_key: str = "SO1",
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    status: str = "success",
    timestamp: datetime | None = None,
) -> WriteRecord:
    return ledger.record_write(
        trace_id=trace_id,
        form_id="SAL_Order",
        business_key=business_key,
        action="edit",
        provenance=_provenance(),
        before_snapshot=before,
        after_snapshot=after,
        status=status,  # type: ignore[arg-type]
        timestamp=timestamp or datetime(2026, 10, 18, 10, 0),
    )


# ── 字段 diff ──


def test_diff_top_level_change() -> None:
    diffs = compute_field_diff({"FAmount": 100, "FRemark": "a"}, {"FAmount": 80, "FRemark": "a"})

    assert len(diffs) == 1
    assert diffs[0].field_name == "FAmount"
    assert diffs[0].old_value == 100
    assert diffs[0].new_value == 80


def test_diff_nested_uses_dotted_path() -> None:
    diffs = compute_field_diff(
        {"customer": {"number": "C1", "name": "A"}},
        {"customer": {"number": "C1", "name": "B"}},
    )

    assert [item.field_name for item in diffs] == ["customer.name"]
    assert diffs[0].old_value == "A"
    assert diffs[0].new_value == "B"


def test_diff_create_and_delete() -> None:
    created = compute_field_diff(None, {"FBillNo": "SO1", "FAmount": 100})
    deleted = compute_field_diff({"FBillNo": "SO1", "FAmount": 100}, None)

    assert [item.field_name for item in created] == ["FAmount", "FBillNo"]
    assert all(item.old_value is None for item in created)
    assert all(item.new_value is None for item in deleted)


def test_diff_ignores_unchanged_values() -> None:
    assert compute_field_diff({"a": 1}, {"a": 1}) == []


# ── 落账与 hash 链 ──


def test_records_form_hash_chain(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)

    first = _record(ledger, before={"FAmount": 100}, after={"FAmount": 80})
    second = _record(
        ledger,
        trace_id="trace-2",
        before={"FAmount": 80},
        after={"FAmount": 60},
    )

    assert first.prev_record_hash == GENESIS_HASH
    assert len(first.record_hash) == 64
    assert second.prev_record_hash == first.record_hash
    result = ledger.verify()
    assert result.valid is True
    assert result.checked == 2


def test_failed_write_keeps_before_snapshot_without_diff(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)

    record = _record(
        ledger,
        before={"FAmount": 100},
        after=None,
        status="failed",
    )

    assert record.status == "failed"
    assert record.before_snapshot == {"FAmount": 100}
    assert record.after_snapshot is None
    assert record.field_diff == []
    assert ledger.verify().valid is True


def test_sqlite_round_trip_preserves_record(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    store = SqliteWriteLedgerStore(path)
    first = _record(
        WriteLedger(store),
        before={"FAmount": 100},
        after={"FAmount": 80},
    )
    store.close()

    reloaded_store = SqliteWriteLedgerStore(path)
    reloaded = WriteLedger(reloaded_store)
    records = reloaded.records()

    assert len(records) == 1
    record = records[0]
    assert record.record_id == first.record_id
    assert record.before_snapshot == {"FAmount": 100}
    assert record.after_snapshot == {"FAmount": 80}
    assert record.field_diff[0].field_name == "FAmount"
    assert record.provenance.user_id == "u001"
    assert record.timestamp == datetime(2026, 10, 18, 10, 0)
    assert reloaded.verify().valid is True
    reloaded_store.close()


def test_find_by_trace_and_business_key(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)
    _record(ledger, trace_id="t1", business_key="SO1")
    _record(ledger, trace_id="t2", business_key="SO1")
    _record(ledger, trace_id="t1", business_key="SO2")

    assert len(ledger.find_by_trace_id("t1")) == 2
    assert len(ledger.find_by_business_key("SO1")) == 2
    assert ledger.find_by_business_key("SO1")[0].trace_id == "t1"


# ── 篡改与接口约束 ──


def test_tampered_sqlite_record_breaks_chain(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    store = SqliteWriteLedgerStore(path)
    _record(WriteLedger(store), before={"FAmount": 100}, after={"FAmount": 80})
    store.close()

    connection = sqlite3.connect(str(path))
    connection.execute(
        "UPDATE write_records SET after_snapshot = ? WHERE seq = 1",
        ('{"FAmount":999}',),
    )
    connection.commit()
    connection.close()

    reloaded_store = SqliteWriteLedgerStore(path)
    result = WriteLedger(reloaded_store).verify()

    assert result.valid is False
    assert result.broken_index == 0
    assert "内容被篡改" in result.reason
    reloaded_store.close()


def test_empty_ledger_is_valid(tmp_path: Path) -> None:
    ledger, store = _ledger(tmp_path)

    assert store.last_record_hash() == GENESIS_HASH
    result = ledger.verify()
    assert result.valid is True
    assert result.checked == 0


def test_store_has_no_update_or_delete_api(tmp_path: Path) -> None:
    _, store = _ledger(tmp_path)

    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")
    store.close()


def test_record_is_json_serializable(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)

    record = _record(ledger, before={"FAmount": 100}, after={"FAmount": 80})
    payload = json.loads(json.dumps(record.to_hash_payload()))

    assert payload["record_id"] == record.record_id
    assert payload["field_diff"][0]["new_value"] == 80
    assert payload["prev_record_hash"] == GENESIS_HASH
