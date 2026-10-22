"""M9-T05：ApprovalExecutor 恢复执行单测。"""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from governance.approval import ApprovalService, FileApprovalStore
from governance.approval_executor import ApprovalExecutor
from infrastructure.exceptions import ApprovalError
from kingdee.write_coordinator import WriteCoordinator
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.agent import AgentTrace
from models.approval import PendingAction
from models.user import UserContext
from observability.tracing import TraceStore

REQUESTER = UserContext(user_id="alice", role="salesperson")
MANAGER_A = UserContext(user_id="mgr-a", role="manager")
MANAGER_B = UserContext(user_id="mgr-b", role="manager")


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class FakeExecutor:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail_first = fail_first
        self._failed = False

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((tool_name, dict(arguments)))
        if self._fail_first and not self._failed:
            self._failed = True
            raise RuntimeError("write failed")
        return {"success": True}


class FakeSnapshots:
    async def before(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        return {"FBillNo": "SO000001"}

    async def after(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        return {"FBillNo": "SO000001"}


async def _make_approved(
    tmp_path: Path,
    *,
    trace_id: str | None = None,
    form_id: str = "SAL_Order",
    business_key: str = "SO000001",
) -> tuple[ApprovalService, PendingAction]:
    service = ApprovalService(FileApprovalStore(tmp_path / "approvals"), clock=_Clock())
    action = await service.create(
        trace_id=trace_id or str(uuid4()),
        session_id="sess-exec",
        requester=REQUESTER,
        tool_name="erp_submit_sales_order",
        arguments={"bill_no": "SO000001"},
        summary="提交销售订单 SO000001",
        form_id=form_id,
        business_key=business_key,
        risk_score=70.0,
        risk_level="dual_approve",
        idempotency_key=f"exec-{uuid4().hex}",
        write_action="submit",
    )
    await service.approve(action.approval_id, MANAGER_A)
    approved = await service.approve(action.approval_id, MANAGER_B)
    assert approved.status == "approved"
    return service, approved


def _writer(tmp_path: Path) -> tuple[WriteCoordinator, WriteLedger]:
    ledger = WriteLedger(SqliteWriteLedgerStore(tmp_path / "ledger.sqlite3"))
    return WriteCoordinator(ledger), ledger


def _executor(
    tmp_path: Path,
    service: ApprovalService,
    fake: FakeExecutor,
) -> tuple[ApprovalExecutor, WriteLedger]:
    writer, ledger = _writer(tmp_path)
    executor = ApprovalExecutor(
        service,
        trace_store=TraceStore(tmp_path / "traces"),
        writer=writer,
        executor=fake,
        snapshots=FakeSnapshots(),
        ledger=ledger,
    )
    return executor, ledger


def _save_trace(
    tmp_path: Path,
    action: PendingAction,
    *,
    user_id: str = "alice",
) -> None:
    user = REQUESTER if user_id == "alice" else UserContext(user_id="bob", role="operator")
    TraceStore(tmp_path / "traces").save(
        AgentTrace(
            trace_id=UUID(action.trace_id),
            user_input="提交销售订单 SO000001",
            user_context=user,
            user_id=user_id,
            session_id="sess-exec",
        )
    )


async def test_execute_once_and_idempotent_retry(tmp_path: Path) -> None:
    service, action = await _make_approved(tmp_path)
    _save_trace(tmp_path, action)
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    first = await executor.execute(action.approval_id)
    second = await executor.execute(action.approval_id)

    assert first.execution_status == "succeeded"
    assert second.execution_status == "succeeded"
    assert len(fake.calls) == 1
    assert first.execution_result["status"] == "success"


async def test_concurrent_execute_only_once(tmp_path: Path) -> None:
    service, action = await _make_approved(tmp_path)
    _save_trace(tmp_path, action)
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    await asyncio.gather(
        executor.execute(action.approval_id),
        executor.execute(action.approval_id),
    )

    assert len(fake.calls) == 1


async def test_failure_then_retry_succeeds(tmp_path: Path) -> None:
    service, action = await _make_approved(tmp_path)
    _save_trace(tmp_path, action)
    fake = FakeExecutor(fail_first=True)
    executor, _ = _executor(tmp_path, service, fake)

    failed = await executor.execute(action.approval_id)
    retried = await executor.execute(action.approval_id)

    assert failed.execution_status == "failed"
    assert retried.execution_status == "succeeded"
    assert len(fake.calls) == 2


async def test_missing_trace_fails_without_write(tmp_path: Path) -> None:
    service, action = await _make_approved(tmp_path)
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    result = await executor.execute(action.approval_id)

    assert result.execution_status == "failed"
    assert "Trace" in result.execution_error
    assert fake.calls == []


async def test_trace_user_mismatch_fails_without_write(tmp_path: Path) -> None:
    service, action = await _make_approved(tmp_path)
    _save_trace(tmp_path, action, user_id="bob")
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    result = await executor.execute(action.approval_id)

    assert result.execution_status == "failed"
    assert "归属" in result.execution_error
    assert fake.calls == []


async def test_not_approved_cannot_execute(tmp_path: Path) -> None:
    service = ApprovalService(FileApprovalStore(tmp_path / "approvals"), clock=_Clock())
    action = await service.create(
        trace_id=str(uuid4()),
        session_id="sess-exec",
        requester=REQUESTER,
        tool_name="erp_submit_sales_order",
        arguments={"bill_no": "SO000001"},
        summary="提交销售订单",
        form_id="SAL_Order",
        business_key="SO000001",
        idempotency_key="pending-only",
        write_action="submit",
    )
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    with pytest.raises(ApprovalError) as exc:
        await executor.execute(action.approval_id)
    assert exc.value.code == "approval_conflict"
    assert fake.calls == []


async def test_missing_form_or_business_key_fails_without_write(tmp_path: Path) -> None:
    service, action = await _make_approved(tmp_path, form_id="", business_key="")
    _save_trace(tmp_path, action)
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    result = await executor.execute(action.approval_id)

    assert result.execution_status == "failed"
    assert fake.calls == []


async def test_ledger_recovery_skips_write(tmp_path: Path) -> None:
    service, action = await _make_approved(tmp_path)
    _save_trace(tmp_path, action)
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)
    await executor.execute(action.approval_id)

    store = FileApprovalStore(tmp_path / "approvals")
    current = store.load(action.approval_id)
    store.save(current.model_copy(update={"execution_status": "running"}))

    recovered = await executor.execute(action.approval_id)

    assert recovered.execution_status == "succeeded"
    assert len(fake.calls) == 1


async def test_rejected_action_is_not_executed(tmp_path: Path) -> None:
    service = ApprovalService(FileApprovalStore(tmp_path / "approvals"), clock=_Clock())
    action = await service.create(
        trace_id=str(uuid4()),
        session_id="sess-exec",
        requester=REQUESTER,
        tool_name="erp_submit_sales_order",
        arguments={"bill_no": "SO000001"},
        summary="提交销售订单",
        form_id="SAL_Order",
        business_key="SO000001",
        idempotency_key="rejected-exec",
        write_action="submit",
    )
    await service.reject(action.approval_id, MANAGER_A)
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    with pytest.raises(ApprovalError) as exc:
        await executor.execute(action.approval_id)
    assert exc.value.code == "approval_conflict"
    assert fake.calls == []


async def test_expired_action_is_not_executed(tmp_path: Path) -> None:
    clock = _Clock()
    service = ApprovalService(FileApprovalStore(tmp_path / "approvals"), clock=clock)
    action = await service.create(
        trace_id=str(uuid4()),
        session_id="sess-exec",
        requester=REQUESTER,
        tool_name="erp_submit_sales_order",
        arguments={"bill_no": "SO000001"},
        summary="提交销售订单",
        form_id="SAL_Order",
        business_key="SO000001",
        idempotency_key="expired-exec",
        write_action="submit",
    )
    clock.advance(1801)
    fake = FakeExecutor()
    executor, _ = _executor(tmp_path, service, fake)

    with pytest.raises(ApprovalError) as exc:
        await executor.execute(action.approval_id)
    assert exc.value.code == "approval_conflict"
    assert fake.calls == []
