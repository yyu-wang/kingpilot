"""M9-T04：审批单持久化与双人复核状态机单测。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from governance.approval import ApprovalService, FileApprovalStore
from infrastructure.exceptions import ApprovalError
from models.approval import PendingAction
from models.audit import AuditEvent
from models.user import UserContext
from observability.audit import AuditLogger

REQUESTER = UserContext(user_id="alice", role="salesperson")
MANAGER_A = UserContext(user_id="mgr-a", role="manager")
MANAGER_B = UserContext(user_id="mgr-b", role="manager")
ADMIN = UserContext(user_id="admin", role="admin")
VIEWER = UserContext(user_id="viewer", role="viewer")
MANAGER_REQUESTER = UserContext(user_id="mgr-req", role="manager")


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class _ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _service(tmp_path: Path, clock: _Clock, audit: AuditLogger | None = None) -> ApprovalService:
    return ApprovalService(
        FileApprovalStore(tmp_path / "approvals"),
        audit=audit,
        ttl_seconds=1800,
        clock=clock,
    )


async def _create(
    service: ApprovalService,
    *,
    requester: UserContext = REQUESTER,
    key: str = "idem-1",
) -> PendingAction:
    return await service.create(
        trace_id="trace-1",
        session_id="sess-1",
        requester=requester,
        tool_name="kingdee_update_sales_order",
        arguments={"bill_no": "SO000001", "amount": 120000, "phone": "13800138000"},
        summary="修改销售订单 SO000001，金额 120000",
        form_id="SAL_Order",
        business_key="SO000001",
        risk_score=62.0,
        risk_level="dual_approve",
        required_approvers=["sales_manager"],
        idempotency_key=key,
    )


async def test_create_persists_pending_action(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)

    action = await _create(service)

    assert action.status == "pending"
    assert action.required_approvals == 2
    store = FileApprovalStore(tmp_path / "approvals")
    path = store.path_for(action.approval_id)
    assert path.exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    loaded = store.load(action.approval_id)
    assert loaded.status == "pending"
    assert loaded.arguments["bill_no"] == "SO000001"


async def test_two_distinct_approvals_approve(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service)

    first = await service.approve(action.approval_id, MANAGER_A)
    assert first.status == "pending"
    assert len(first.decisions) == 1

    second = await service.approve(action.approval_id, MANAGER_B)
    assert second.status == "approved"
    assert len(second.decisions) == 2


async def test_same_approver_is_idempotent(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service)

    first = await service.approve(action.approval_id, MANAGER_A)
    second = await service.approve(action.approval_id, MANAGER_A)

    assert second.status == "pending"
    assert len(second.decisions) == len(first.decisions) == 1


async def test_requester_cannot_self_approve(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service, requester=MANAGER_REQUESTER)

    with pytest.raises(ApprovalError) as exc:
        await service.approve(action.approval_id, MANAGER_REQUESTER)
    assert exc.value.code == "approval_forbidden"


async def test_viewer_cannot_approve(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service)

    with pytest.raises(ApprovalError) as exc:
        await service.approve(action.approval_id, VIEWER)
    assert exc.value.code == "approval_forbidden"


async def test_reject_is_terminal_and_idempotent(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service)

    rejected = await service.reject(action.approval_id, MANAGER_A, "金额不对")
    assert rejected.status == "rejected"
    again = await service.reject(action.approval_id, MANAGER_A)
    assert again.status == "rejected"

    with pytest.raises(ApprovalError) as exc:
        await service.approve(action.approval_id, MANAGER_B)
    assert exc.value.code == "approval_conflict"


async def test_expired_action_transitions_and_blocks(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service)

    clock.advance(1801)
    expired = await service.get(action.approval_id, REQUESTER)
    assert expired.status == "expired"

    with pytest.raises(ApprovalError) as exc:
        await service.approve(action.approval_id, MANAGER_A)
    assert exc.value.code == "approval_expired"


async def test_restart_recovers_pending_action(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    first = _service(tmp_path, clock)
    action = await _create(first)

    second = _service(tmp_path, clock)
    loaded = await second.get(action.approval_id, MANAGER_A)
    assert loaded.status == "pending"
    approved = await second.approve(action.approval_id, MANAGER_A)
    assert approved.status == "pending"


async def test_idempotency_key_deduplicates_create(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)

    first = await _create(service)
    second = await _create(service)

    assert first.approval_id == second.approval_id
    assert len(list((tmp_path / "approvals").glob("*.json"))) == 1


async def test_corrupt_file_is_skipped_in_list(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    await _create(service)
    (tmp_path / "approvals" / "bad.json").write_text("{bad", encoding="utf-8")

    inbox = await service.list_for_user(MANAGER_A, scope="inbox")

    assert len(inbox) == 1


async def test_list_scopes(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    await _create(service)

    manager_inbox = await service.list_for_user(MANAGER_A, scope="inbox")
    viewer_inbox = await service.list_for_user(VIEWER, scope="inbox")
    mine = await service.list_for_user(REQUESTER, scope="mine")
    admin_all = await service.list_for_user(ADMIN, scope="all")

    assert len(manager_inbox) == 1
    assert viewer_inbox == []
    assert len(mine) == 1
    assert len(admin_all) == 1

    with pytest.raises(ApprovalError) as exc:
        await service.list_for_user(MANAGER_A, scope="all")
    assert exc.value.code == "approval_forbidden"


async def test_get_visibility(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service)

    assert (await service.get(action.approval_id, MANAGER_A)).approval_id == action.approval_id
    assert (await service.get(action.approval_id, REQUESTER)).approval_id == action.approval_id
    with pytest.raises(ApprovalError) as exc:
        await service.get(action.approval_id, VIEWER)
    assert exc.value.code == "approval_not_found"


async def test_comment_is_redacted(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    service = _service(tmp_path, clock)
    action = await _create(service)

    updated = await service.approve(
        action.approval_id,
        MANAGER_A,
        "联系电话 13800138000",
    )

    assert updated.decisions[0].comment == "联系电话 138****8000"
    raw = json.loads(
        (tmp_path / "approvals" / f"{action.approval_id}.json").read_text(encoding="utf-8")
    )
    assert "13800138000" not in raw["approval"]["decisions"][0]["comment"]


async def test_audit_events_and_no_arguments(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    sink = _ListSink()
    service = _service(tmp_path, clock, AuditLogger(sink))
    action = await _create(service)

    await service.approve(action.approval_id, MANAGER_A)
    await service.approve(action.approval_id, MANAGER_B)

    event_types = [event.event_type for event in sink.events]
    assert event_types == [
        "approval.requested",
        "approval.approved",
        "approval.approved",
    ]
    assert sink.events[-1].status == "success"
    dump = json.dumps(
        [event.model_dump(mode="json") for event in sink.events],
        ensure_ascii=False,
    )
    assert "13800138000" not in dump
    assert "arguments" not in dump


async def test_expiry_writes_audit_event(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 10, 19, 10, 0, tzinfo=UTC))
    sink = _ListSink()
    service = _service(tmp_path, clock, AuditLogger(sink))
    action = await _create(service)

    clock.advance(1801)
    await service.get(action.approval_id, MANAGER_A)

    assert sink.events[-1].event_type == "approval.expired"
    assert sink.events[-1].status == "blocked"
