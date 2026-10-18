"""M5-T04：链式 hash 单测。

覆盖确定性、hash 字段排除、合法链、内容篡改、前序篡改、删除/换序、
缺字段与空链。
"""

import json
from datetime import datetime
from typing import Any

from ledger.hash_chain import (
    GENESIS_HASH,
    ChainVerification,
    compute_record_hash,
    verify_chain,
)


def _build_chain(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按顺序构造合法链。"""
    records: list[dict[str, Any]] = []
    previous = GENESIS_HASH
    for index, payload in enumerate(payloads):
        record = {"record_id": str(index), **payload, "prev_record_hash": previous}
        record["record_hash"] = compute_record_hash(record, previous)
        previous = record["record_hash"]
        records.append(record)
    return records


def _replace(records: list[dict[str, Any]], index: int, **updates: Any) -> list[dict[str, Any]]:
    tampered = [dict(record) for record in records]
    tampered[index].update(updates)
    return tampered


# ── hash 计算 ──


def test_hash_is_deterministic_and_order_independent() -> None:
    first = compute_record_hash({"a": 1, "b": {"x": 2, "y": 3}}, GENESIS_HASH)
    second = compute_record_hash({"b": {"y": 3, "x": 2}, "a": 1}, GENESIS_HASH)

    assert first == second
    assert len(first) == 64


def test_hash_changes_with_payload_or_previous() -> None:
    base = compute_record_hash({"a": 1}, GENESIS_HASH)

    assert base != compute_record_hash({"a": 2}, GENESIS_HASH)
    assert base != compute_record_hash({"a": 1}, "f" * 64)


def test_hash_fields_are_excluded_from_payload() -> None:
    base = compute_record_hash({"a": 1}, GENESIS_HASH)
    with_hashes = compute_record_hash(
        {"a": 1, "record_hash": "junk", "prev_record_hash": "junk"}, GENESIS_HASH
    )

    assert base == with_hashes


def test_non_json_value_is_stable() -> None:
    moment = datetime(2026, 10, 18, 10, 0)

    first = compute_record_hash({"at": moment}, GENESIS_HASH)
    second = compute_record_hash({"at": moment}, GENESIS_HASH)

    assert first == second


# ── 链校验 ──


def test_valid_chain_passes() -> None:
    records = _build_chain([{"amount": 100}, {"amount": 200}, {"amount": 300}])

    result = verify_chain(records)

    assert result.valid is True
    assert result.checked == 3
    assert result.broken_index is None
    assert json.loads(json.dumps(result.to_dict()))["valid"] is True


def test_empty_chain_is_valid() -> None:
    result = verify_chain([])

    assert result.valid is True
    assert result.checked == 0


def test_tampered_payload_is_detected() -> None:
    records = _build_chain([{"amount": 100}, {"amount": 200}, {"amount": 300}])
    tampered = _replace(records, 1, amount=999)

    result = verify_chain(tampered)

    assert result.valid is False
    assert result.broken_index == 1
    assert "内容被篡改" in result.reason


def test_tampered_previous_hash_is_detected() -> None:
    records = _build_chain([{"amount": 100}, {"amount": 200}, {"amount": 300}])
    tampered = _replace(records, 2, prev_record_hash="f" * 64)

    result = verify_chain(tampered)

    assert result.valid is False
    assert result.broken_index == 2
    assert "前序 hash 不匹配" in result.reason


def test_deleted_record_is_detected() -> None:
    records = _build_chain([{"amount": 100}, {"amount": 200}, {"amount": 300}])

    result = verify_chain([records[0], records[2]])

    assert result.valid is False
    assert result.broken_index == 1
    assert "删除或换序" in result.reason


def test_reordered_records_are_detected() -> None:
    records = _build_chain([{"amount": 100}, {"amount": 200}, {"amount": 300}])

    result = verify_chain([records[0], records[2], records[1]])

    assert result.valid is False
    assert result.broken_index == 1


def test_missing_hash_fields_are_detected() -> None:
    result = verify_chain([{"record_id": "x"}])

    assert result.valid is False
    assert result.broken_index == 0
    assert "缺少" in result.reason


def test_genesis_hash_shape() -> None:
    assert GENESIS_HASH == "0" * 64
    assert isinstance(ChainVerification(valid=True, checked=0).to_dict(), dict)
