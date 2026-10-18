"""M5-T01：幂等层单测。

覆盖 Key 确定性、顺序无关、同 Key 去重、并发竞争、失败重试、
TTL 过期与 processing 冲突 fail-closed。
"""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from infrastructure.exceptions import IdempotencyError
from kingdee.idempotency import (
    IdempotencyRecord,
    IdempotencyService,
    InMemoryIdempotencyStore,
    build_idempotency_key,
)


class FakeClock:
    """可推进的假时钟。"""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ── Key 生成 ──


def test_key_is_order_independent() -> None:
    first = build_idempotency_key("u001", "tool", {"a": 1, "b": {"x": 2, "y": 3}}, time_bucket=1)
    second = build_idempotency_key("u001", "tool", {"b": {"y": 3, "x": 2}, "a": 1}, time_bucket=1)

    assert first == second
    assert len(first) == 64


def test_key_changes_with_inputs() -> None:
    base = build_idempotency_key("u001", "tool", {"a": 1}, time_bucket=1)

    assert base != build_idempotency_key("u002", "tool", {"a": 1}, time_bucket=1)
    assert base != build_idempotency_key("u001", "other", {"a": 1}, time_bucket=1)
    assert base != build_idempotency_key("u001", "tool", {"a": 2}, time_bucket=1)
    assert base != build_idempotency_key("u001", "tool", {"a": 1}, time_bucket=2)


def test_key_handles_non_json_values() -> None:
    key = build_idempotency_key(
        "u001", "tool", {"at": datetime(2026, 10, 18, 10, 0)}, time_bucket=0
    )

    assert len(key) == 64


# ── 去重与并发 ──


async def test_same_key_executes_once() -> None:
    service = IdempotencyService(ttl_seconds=60, clock=FakeClock())
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    assert await service.run("key", operation) == "ok"
    assert await service.run("key", operation) == "ok"
    assert calls == 1


async def test_concurrent_same_key_executes_once() -> None:
    service = IdempotencyService(ttl_seconds=60, clock=FakeClock())
    calls = 0

    async def operation() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)  # 让出控制权，真正制造并发竞争
        return calls

    results = await asyncio.gather(*(service.run("key", operation) for _ in range(5)))

    assert calls == 1
    assert results == [1, 1, 1, 1, 1]


async def test_run_once_dedupes_by_user_tool_and_arguments() -> None:
    clock = FakeClock(0)
    service = IdempotencyService(ttl_seconds=60, clock=clock)
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return f"call-{calls}"

    first = await service.run_once(
        user_id="u001", tool_name="create", arguments={"a": 1}, operation=operation
    )
    second = await service.run_once(
        user_id="u001", tool_name="create", arguments={"a": 1}, operation=operation
    )
    third = await service.run_once(
        user_id="u002", tool_name="create", arguments={"a": 1}, operation=operation
    )

    assert first == second == "call-1"
    assert third == "call-2"
    assert calls == 2


# ── 失败与 TTL ──


async def test_failed_operation_can_retry() -> None:
    store = InMemoryIdempotencyStore()
    clock = FakeClock(0)
    service = IdempotencyService(store, ttl_seconds=60, clock=clock)
    calls = 0

    async def failing() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    async def succeeding() -> str:
        nonlocal calls
        calls += 1
        return "done"

    with pytest.raises(RuntimeError, match="boom"):
        await service.run("key", failing)

    record = store.get("key", clock.now)
    assert record is not None
    assert record.status == "failed"
    assert "boom" in record.error

    assert await service.run("key", succeeding) == "done"
    assert calls == 2


async def test_ttl_expiry_allows_reexecution() -> None:
    clock = FakeClock(0)
    service = IdempotencyService(ttl_seconds=10, clock=clock)
    calls = 0

    async def operation() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await service.run("key", operation) == 1
    clock.advance(9)
    assert await service.run("key", operation) == 1
    clock.advance(1)  # 到期
    assert await service.run("key", operation) == 2
    assert calls == 2


def test_time_bucket_changes_after_ttl() -> None:
    clock = FakeClock(0)
    service = IdempotencyService(ttl_seconds=10, clock=clock)

    first = service.build_key("u001", "tool", {"a": 1})
    clock.advance(10)
    second = service.build_key("u001", "tool", {"a": 1})

    assert first != second


async def test_processing_record_fails_closed() -> None:
    store = InMemoryIdempotencyStore()
    store.put(IdempotencyRecord(key="key", status="processing", expires_at=100))
    service = IdempotencyService(store, ttl_seconds=60, clock=FakeClock(1))
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "should-not-run"

    with pytest.raises(IdempotencyError, match="正在处理中"):
        await service.run("key", operation)

    assert calls == 0


async def test_expired_record_is_cleaned_on_read() -> None:
    store = InMemoryIdempotencyStore()
    store.put(IdempotencyRecord(key="key", status="success", expires_at=10, result="old"))

    assert store.get("key", 9) is not None
    assert store.get("key", 10) is None


async def test_cached_result_is_returned_unchanged() -> None:
    service = IdempotencyService(ttl_seconds=60, clock=FakeClock())
    payload: dict[str, Any] = {"id": 1001, "ok": True}

    async def operation() -> dict[str, Any]:
        return payload

    first = await service.run("key", operation)
    second = await service.run("key", operation)

    assert first is payload
    assert second is payload
