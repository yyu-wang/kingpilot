"""M5-T06：写操作协调器端到端单测。

覆盖成功、失败回滚、写前快照缺失、写后快照失败、幂等重跑、
证据链报告与装配校验。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from kingdee.idempotency import IdempotencyService
from kingdee.saga import SagaStep
from kingdee.write_coordinator import WriteCoordinator, WriteOperation
from ledger.provenance import ProvenanceReporter
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.agent import AgentTrace
from models.base import SagaStatus
from models.user import UserContext


class FakeExecutor:
    """记录调用顺序；可指定失败的正向工具。"""

    def __init__(self, failures: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._failures = failures or set()

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        payload = dict(arguments)
        self.calls.append((tool_name, payload))
        if tool_name in self._failures:
            raise RuntimeError(f"{tool_name} failed")
        return {"success": True, "tool": tool_name}


class FakeSnapshots:
    """按工具名返回预设快照；可指定查询失败的工具。"""

    def __init__(
        self,
        before: dict[str, dict[str, Any] | None] | None = None,
        after: dict[str, dict[str, Any] | None] | None = None,
        fail_before: set[str] | None = None,
        fail_after: set[str] | None = None,
    ) -> None:
        self._before = before or {}
        self._after = after or {}
        self._fail_before = fail_before or set()
        self._fail_after = fail_after or set()

    async def before(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        if tool_name in self._fail_before:
            raise RuntimeError("before snapshot failed")
        return self._before.get(tool_name)

    async def after(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        if tool_name in self._fail_after:
            raise RuntimeError("after snapshot failed")
        return self._after.get(tool_name)


def _trace() -> AgentTrace:
    return AgentTrace(
        user_input="把 SO1 提交审核",
        user_context=UserContext(user_id="u001", role="operator"),
        user_id="u001",
        session_id="s1",
        llm_planning={"reasoning": "先创建再提交审核"},
        guard_checks=[{"guard": "guard_permission", "passed": True, "reason": ""}],
        risk_assessment={
            "score": 42.0,
            "level": "confirm",
            "hitl": {"approved": True, "level": "confirm"},
        },
        permission_check={"required_approvers": []},
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
        WriteOperation(
            SagaStep(
                "s3",
                "audit",
                {"bill_no": "SO1"},
                action="audit",
                compensate_tool_name="comp_unaudit",
            ),
            form_id="SAL_Order",
            business_key="SO1",
        ),
    ]


def _snapshots() -> FakeSnapshots:
    return FakeSnapshots(
        before={
            "create": None,
            "submit": {"FDocumentStatus": "A"},
            "audit": {"FDocumentStatus": "C"},
        },
        after={
            "create": {"FBillNo": "SO1", "FDocumentStatus": "A"},
            "submit": {"FDocumentStatus": "C"},
            "audit": {"FDocumentStatus": "C"},
        },
    )


def _ledger(tmp_path: Path) -> WriteLedger:
    return WriteLedger(SqliteWriteLedgerStore(tmp_path / "ledger.db"))


def _coordinator(ledger: WriteLedger) -> WriteCoordinator:
    return WriteCoordinator(
        ledger,
        idempotency=IdempotencyService(ttl_seconds=3600, clock=lambda: 0.0),
        compensate_map={},
    )


# ── 成功与失败回滚 ──


async def test_all_steps_succeed_and_land_in_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    executor = FakeExecutor()

    result = await _coordinator(ledger).execute(
        _trace(), _operations(), executor, _snapshots(), saga_id="saga-1"
    )

    assert result.saga.status == SagaStatus.SUCCESS
    assert len(result.records) == 3
    first = result.records[0]
    assert first.status == "success"
    assert first.before_snapshot is None
    assert first.after_snapshot == {"FBillNo": "SO1", "FDocumentStatus": "A"}
    assert first.field_diff
    assert result.records[1].before_snapshot == {"FDocumentStatus": "A"}
    assert ledger.verify().valid is True


async def test_second_step_failure_rolls_back_and_records_failure(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    executor = FakeExecutor(failures={"submit"})

    result = await _coordinator(ledger).execute(
        _trace(), _operations(), executor, _snapshots(), saga_id="saga-2"
    )

    assert result.saga.status == SagaStatus.ROLLED_BACK
    assert [record.status for record in result.records] == ["success", "failed"]
    failed = result.records[1]
    assert failed.before_snapshot == {"FDocumentStatus": "A"}
    assert failed.after_snapshot is None
    assert failed.field_diff == []
    assert ("comp_delete", {"bill_no": "SO1"}) in executor.calls
    assert ledger.verify().valid is True


async def test_missing_before_snapshot_aborts_edit(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    executor = FakeExecutor()
    operations = [
        _operations()[0],
        WriteOperation(
            SagaStep(
                "s2",
                "edit",
                {"bill_no": "SO1"},
                action="edit",
                compensate_tool_name="comp_revert",
            ),
            form_id="SAL_Order",
            business_key="SO1",
        ),
    ]
    snapshots = FakeSnapshots(
        before={"create": None, "edit": None},
        after={"create": {"FBillNo": "SO1"}},
    )

    result = await _coordinator(ledger).execute(
        _trace(), operations, executor, snapshots, saga_id="saga-3"
    )

    assert result.saga.status == SagaStatus.ROLLED_BACK
    assert [record.status for record in result.records] == ["success"]
    assert "edit" not in [name for name, _ in executor.calls]
    assert ("comp_delete", {"bill_no": "SO1"}) in executor.calls


async def test_before_snapshot_exception_aborts_edit(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    executor = FakeExecutor()
    operations = [
        _operations()[0],
        WriteOperation(
            SagaStep("s2", "edit", {"bill_no": "SO1"}, action="edit"),
            form_id="SAL_Order",
            business_key="SO1",
        ),
    ]
    snapshots = FakeSnapshots(before={"create": None}, fail_before={"edit"})

    result = await _coordinator(ledger).execute(
        _trace(), operations, executor, snapshots, saga_id="saga-4"
    )

    assert result.saga.status == SagaStatus.ROLLED_BACK
    assert "edit" not in [name for name, _ in executor.calls]


async def test_after_snapshot_failure_still_records_success(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    executor = FakeExecutor()
    snapshots = FakeSnapshots(
        before={"create": None},
        after={"create": {"FBillNo": "SO1"}},
        fail_after={"create"},
    )
    operation = WriteOperation(
        SagaStep("s1", "create", {"bill_no": "SO1"}, action="save"),
        form_id="SAL_Order",
        business_key="SO1",
    )

    result = await _coordinator(ledger).execute(
        _trace(), [operation], executor, snapshots, saga_id="saga-5"
    )

    assert result.saga.status == SagaStatus.SUCCESS
    assert result.records[0].status == "success"
    assert result.records[0].after_snapshot is None
    assert ledger.verify().valid is True


async def test_before_snapshot_failure_for_save_continues(tmp_path: Path) -> None:
    """新建允许写前快照失败（没有原状态可查）。"""
    ledger = _ledger(tmp_path)
    executor = FakeExecutor()
    snapshots = FakeSnapshots(
        before={"create": None},
        after={"create": {"FBillNo": "SO1"}},
        fail_before={"create"},
    )
    operation = WriteOperation(
        SagaStep("s1", "create", {"bill_no": "SO1"}, action="save"),
        form_id="SAL_Order",
        business_key="SO1",
    )

    result = await _coordinator(ledger).execute(
        _trace(), [operation], executor, snapshots, saga_id="saga-save-fail"
    )

    assert result.saga.status == SagaStatus.SUCCESS
    assert result.records[0].before_snapshot is None


class _RawExecutor:
    """返回非 Mapping 结果的执行器（覆盖响应归一化）。"""

    def __init__(self, result: Any) -> None:
        self._result = result

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        return self._result


@pytest.mark.parametrize(
    ("response", "expected"),
    [(None, {}), ("ok", {"result": "ok"})],
)
async def test_non_mapping_response_is_normalized(
    tmp_path: Path, response: Any, expected: dict[str, Any]
) -> None:
    ledger = _ledger(tmp_path)
    operation = WriteOperation(
        SagaStep("s1", "create", {"bill_no": "SO1"}, action="save"),
        form_id="SAL_Order",
        business_key="SO1",
    )

    result = await _coordinator(ledger).execute(
        _trace(), [operation], _RawExecutor(response), _snapshots(), saga_id="saga-raw"
    )

    assert result.records[0].provenance.api_response == expected


async def test_invalid_action_fails_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    operation = WriteOperation(
        SagaStep("s1", "create", {"bill_no": "SO1"}, action="bogus"),
        form_id="SAL_Order",
        business_key="SO1",
    )

    result = await _coordinator(ledger).execute(
        _trace(), [operation], FakeExecutor(), _snapshots(), saga_id="saga-bad-action"
    )

    assert result.saga.status == SagaStatus.FAILED
    assert "不支持的写动作" in result.saga.step_results[0].error


# ── 幂等重跑与报告 ──


async def test_repeat_saga_does_not_repeat_successful_writes(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    executor = FakeExecutor(failures={"submit"})
    coordinator = _coordinator(ledger)

    first = await coordinator.execute(
        _trace(), _operations(), executor, _snapshots(), saga_id="saga-repeat"
    )
    second = await coordinator.execute(
        _trace(), _operations(), executor, _snapshots(), saga_id="saga-repeat"
    )

    assert first.saga.status == SagaStatus.ROLLED_BACK
    assert second.saga.status == SagaStatus.ROLLED_BACK
    assert [name for name, _ in executor.calls] == [
        "create",
        "submit",
        "comp_delete",
        "submit",
    ]
    assert len(ledger.records()) == 3  # create 成功 + 两次 submit 失败
    assert ledger.verify().valid is True


async def test_provenance_report_by_trace(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    trace = _trace()

    await _coordinator(ledger).execute(
        trace, [_operations()[0]], FakeExecutor(), _snapshots(), saga_id="saga-6"
    )
    report = ProvenanceReporter(ledger).report_by_trace_id(str(trace.trace_id))

    assert "用户原话：把 SO1 提交审核" in report
    assert "先创建再提交审核" in report
    assert "guard_permission: ✅ 通过" in report
    assert "风险评分：42.0（confirm）" in report


# ── 装配校验 ──


async def test_empty_operations_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    with pytest.raises(ValueError, match="至少需要一个操作"):
        await _coordinator(ledger).execute(_trace(), [], FakeExecutor(), _snapshots())


async def test_duplicate_tool_names_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    operation = _operations()[0]

    with pytest.raises(ValueError, match="不能重复"):
        await _coordinator(ledger).execute(
            _trace(), [operation, operation], FakeExecutor(), _snapshots()
        )


def test_write_operation_requires_metadata() -> None:
    with pytest.raises(ValueError, match="form_id"):
        WriteOperation(SagaStep("s1", "create", {}), form_id="", business_key="SO1")
    with pytest.raises(ValueError, match="business_key"):
        WriteOperation(SagaStep("s1", "create", {}), form_id="SAL_Order", business_key="")
