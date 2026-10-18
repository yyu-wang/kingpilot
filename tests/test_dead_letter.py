"""M6-T06：死信队列单测。

覆盖文件存储、入队条件、人工重放失败/成功、重复重放保护，
以及 WriteCoordinator 在补偿失败时自动入队。
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from infrastructure.exceptions import DeadLetterError
from kingdee.idempotency import IdempotencyService
from kingdee.saga import SagaResult, SagaStep, SagaStepResult
from kingdee.write_coordinator import WriteCoordinator, WriteOperation
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.agent import AgentTrace
from models.base import SagaStatus
from models.dead_letter import DeadLetter
from models.user import UserContext
from observability.dead_letter import DeadLetterService, FileDeadLetterStore


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeExecutor:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._failures = failures or set()

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        if tool_name in self._failures:
            raise RuntimeError(f"{tool_name} failed")
        return {"success": True, "tool": tool_name}


class FakeSnapshots:
    async def before(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        return None if tool_name == "create" else {"FDocumentStatus": "A"}

    async def after(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        return {"FDocumentStatus": "C"}


def _letter(
    dl_id: str = "dl1",
    *,
    status: str = "pending",
    hour: int = 10,
) -> DeadLetter:
    return DeadLetter(
        dl_id=dl_id,
        saga_id="saga-1",
        trace_id="trace-1",
        created_at=datetime(2026, 10, 18, hour, 0, tzinfo=UTC),
        status=status,
        original_error="boom",
    )


def _saga(
    *,
    status: SagaStatus = SagaStatus.FAILED,
    steps: tuple[SagaStepResult, ...] | None = None,
    compensations: tuple[SagaStepResult, ...] = (),
    error: str = "submit failed",
) -> SagaResult:
    return SagaResult(
        saga_id="saga-1",
        trace_id="trace-1",
        status=status,
        step_results=steps
        or (
            SagaStepResult(step_id="s1", tool_name="create", status=SagaStatus.SUCCESS),
            SagaStepResult(
                step_id="s2", tool_name="submit", status=SagaStatus.FAILED, error="boom"
            ),
        ),
        compensation_results=compensations,
        error=error,
    )


# ── 文件存储 ──


def test_store_roundtrip(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    store.save(_letter())

    loaded = store.get("dl1")

    assert loaded.saga_id == "saga-1"
    assert loaded.original_error == "boom"
    assert store.directory.exists()


def test_store_rejects_invalid_id(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")

    with pytest.raises(DeadLetterError, match="ID 非法"):
        store.save(_letter("../escape"))


def test_store_get_fail_closed(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")

    with pytest.raises(DeadLetterError, match="不存在"):
        store.get("missing")

    store.directory.mkdir(parents=True)
    (store.directory / "bad.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(DeadLetterError, match="损坏"):
        store.get("bad")

    (store.directory / "schema.json").write_text(
        '{"schema_version": 99, "letter": {}}', encoding="utf-8"
    )
    with pytest.raises(DeadLetterError, match="schema"):
        store.get("schema")


def test_store_lists_pending_and_skips_corrupt(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    store.save(_letter("pending", hour=10))
    store.save(_letter("resolved", status="resolved", hour=11))
    (store.directory / "broken.json").write_text("not-json", encoding="utf-8")

    pending = store.list_pending()
    all_letters = store.list_all()

    assert [letter.dl_id for letter in pending] == ["pending"]
    assert [letter.dl_id for letter in all_letters] == ["resolved", "pending"]


def test_store_list_all_handles_missing_directory(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "missing")

    assert store.list_all() == []


def test_store_save_failure_is_wrapped(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    store.path_for("dl1").mkdir(parents=True)

    with pytest.raises(DeadLetterError, match="写入失败"):
        store.save(_letter())


def test_store_get_rejects_bad_payload_shapes(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    store.directory.mkdir(parents=True)
    (store.directory / "missing.json").write_text('{"schema_version": 1}', encoding="utf-8")
    (store.directory / "invalid.json").write_text(
        '{"schema_version": 1, "letter": {"dl_id": "invalid"}}', encoding="utf-8"
    )
    (store.directory / "mismatch.json").write_text(
        '{"schema_version": 1, "letter": ' + json.dumps(_letter("other").to_dict()) + "}",
        encoding="utf-8",
    )

    with pytest.raises(DeadLetterError, match="内容缺失"):
        store.get("missing")
    with pytest.raises(DeadLetterError, match="内容不合法"):
        store.get("invalid")
    with pytest.raises(DeadLetterError, match="ID 与文件名不一致"):
        store.get("mismatch")


# ── 入队条件 ──


def test_enqueue_failed_saga_with_successful_step(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    service = DeadLetterService(store, clock=FakeClock())

    letter = service.enqueue_from_saga(
        _saga(
            compensations=(
                SagaStepResult(
                    step_id="s1",
                    tool_name="comp_delete",
                    status=SagaStatus.FAILED,
                    error="comp failed",
                ),
            )
        ),
        request={"operations": [{"tool_name": "create"}]},
    )

    assert letter is not None
    assert letter.original_error == "submit failed"
    assert "comp_delete" in letter.compensation_error
    assert "人工" in letter.manual_action
    assert letter.request["operations"][0]["tool_name"] == "create"
    assert letter.saga_snapshot["status"] == "failed"
    assert [item.dl_id for item in service.list_pending()] == [letter.dl_id]


def test_enqueue_skips_rolled_back_saga(tmp_path: Path) -> None:
    service = DeadLetterService(FileDeadLetterStore(tmp_path / "dl"), clock=FakeClock())

    letter = service.enqueue_from_saga(_saga(status=SagaStatus.ROLLED_BACK))

    assert letter is None


def test_enqueue_uses_step_error_when_saga_error_empty(tmp_path: Path) -> None:
    service = DeadLetterService(FileDeadLetterStore(tmp_path / "dl"), clock=FakeClock())

    letter = service.enqueue_from_saga(_saga(error=""))

    assert letter is not None
    assert letter.original_error == "boom"


def test_enqueue_skips_saga_without_successful_step(tmp_path: Path) -> None:
    service = DeadLetterService(FileDeadLetterStore(tmp_path / "dl"), clock=FakeClock())
    failed = (SagaStepResult(step_id="s1", tool_name="create", status=SagaStatus.FAILED),)

    letter = service.enqueue_from_saga(_saga(steps=failed))

    assert letter is None


def test_enqueue_marks_manual_intervention(tmp_path: Path) -> None:
    service = DeadLetterService(FileDeadLetterStore(tmp_path / "dl"), clock=FakeClock())

    letter = service.enqueue_from_saga(
        _saga(
            compensations=(
                SagaStepResult(
                    step_id="s1",
                    tool_name="comp_delete",
                    status=SagaStatus.FAILED,
                    error="未配置补偿规则",
                    manual_intervention=True,
                ),
            )
        )
    )

    assert letter is not None
    assert "无法自动补偿" in letter.manual_action


# ── 重放 ──


async def test_replay_success_resolves(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    service = DeadLetterService(store, clock=FakeClock())
    letter = service.enqueue_from_saga(_saga())
    assert letter is not None
    seen: list[DeadLetter] = []

    async def _replay(item: DeadLetter) -> None:
        seen.append(item)

    resolved = await service.replay(letter.dl_id, _replay)

    assert [item.dl_id for item in seen] == [letter.dl_id]
    assert resolved.status == "resolved"
    assert resolved.replay_attempts == 1
    assert resolved.resolved_at == FakeClock()()
    assert service.list_pending() == []


async def test_replay_failure_keeps_pending_then_can_retry(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    service = DeadLetterService(store, clock=FakeClock())
    letter = service.enqueue_from_saga(_saga())
    assert letter is not None

    async def _fail(item: DeadLetter) -> None:
        raise RuntimeError("dependency still down")

    with pytest.raises(DeadLetterError, match="重放失败"):
        await service.replay(letter.dl_id, _fail)

    pending = store.get(letter.dl_id)
    assert pending.status == "pending"
    assert pending.replay_attempts == 1
    assert "dependency still down" in pending.last_replay_error

    async def _ok(item: DeadLetter) -> None:
        return None

    resolved = await service.replay(letter.dl_id, _ok)

    assert resolved.status == "resolved"
    assert resolved.replay_attempts == 2
    assert resolved.last_replay_error == ""


async def test_replay_resolved_letter_rejected(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    service = DeadLetterService(store, clock=FakeClock())
    letter = service.enqueue_from_saga(_saga())
    assert letter is not None
    service.mark_resolved(letter.dl_id, "人工处理")

    async def _ok(item: DeadLetter) -> None:
        return None

    with pytest.raises(DeadLetterError, match="已解决"):
        await service.replay(letter.dl_id, _ok)


def test_mark_resolved(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    service = DeadLetterService(store, clock=FakeClock())
    letter = service.enqueue_from_saga(_saga())
    assert letter is not None

    resolved = service.mark_resolved(letter.dl_id, "已手工冲销")

    assert resolved.status == "resolved"
    assert resolved.resolution_note == "已手工冲销"


# ── Coordinator 集成 ──


def _trace() -> AgentTrace:
    return AgentTrace(
        user_input="创建并提交 SO1",
        user_context=UserContext(user_id="u001", role="operator"),
        user_id="u001",
        session_id="s1",
    )


def _operations() -> list[WriteOperation]:
    return [
        WriteOperation(
            SagaStep(
                "s1",
                "create",
                {"bill_no": "SO1"},
                action="save",
                compensate_tool_name="comp_delete",
            ),
            form_id="SAL_Order",
            business_key="SO1",
        ),
        WriteOperation(
            SagaStep(
                "s2",
                "submit",
                {"bill_no": "SO1"},
                action="submit",
                compensate_tool_name="comp_unsubmit",
            ),
            form_id="SAL_Order",
            business_key="SO1",
        ),
    ]


async def test_coordinator_enqueues_when_compensation_fails(tmp_path: Path) -> None:
    store = FileDeadLetterStore(tmp_path / "dl")
    service = DeadLetterService(store, clock=FakeClock())
    ledger = WriteLedger(SqliteWriteLedgerStore(tmp_path / "ledger.db"))
    coordinator = WriteCoordinator(
        ledger,
        idempotency=IdempotencyService(ttl_seconds=3600, clock=lambda: 0.0),
        dead_letters=service,
    )
    executor = FakeExecutor(failures={"submit", "comp_delete"})

    result = await coordinator.execute(
        _trace(), _operations(), executor, FakeSnapshots(), saga_id="saga-1"
    )

    assert result.saga.status == SagaStatus.FAILED
    assert result.dead_letter is not None
    assert result.dead_letter.saga_id == "saga-1"
    assert len(service.list_pending()) == 1
    assert ("comp_delete", {"bill_no": "SO1"}) in executor.calls
