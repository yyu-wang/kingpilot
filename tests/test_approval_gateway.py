"""M9-T05：审批网关单测（单调用/多调用/字段提取/幂等）。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.tools import ToolDefinition, ToolRegistry
from governance.approval import ApprovalService, FileApprovalStore
from governance.approval_gateway import ApprovalGatewayImpl
from infrastructure.exceptions import ApprovalError
from kingdee.endpoints.sal_order import SAL_ORDER, SalesOrderParams
from models.base import KingPilotBaseModel
from models.llm import ToolCallDecision, ToolSpec
from models.user import UserContext
from safety.approval_gateway import ApprovalRequest
from safety.permission_engine import PermissionDecision
from safety.risk_engine import RiskDecision

_REQUESTER = UserContext(user_id="alice", role="salesperson")


def _no_filter(params: KingPilotBaseModel) -> str:
    return ""


def _write_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name="kingdee_delete_sales_order",
                description="删除销售订单（测试写工具）",
                parameters=SalesOrderParams.model_json_schema(),
            ),
            params_model=SalesOrderParams,
            build_filter=_no_filter,
            endpoint=SAL_ORDER,
            tool_type="write",
            action="delete",
            required_parameters=("bill_no",),
        )
    )
    return registry


def _service(tmp_path: Path) -> ApprovalService:
    return ApprovalService(
        FileApprovalStore(tmp_path / "approvals"),
        clock=lambda: datetime(2026, 10, 19, 10, 0, tzinfo=UTC),
    )


def _request(calls: tuple[ToolCallDecision, ...]) -> ApprovalRequest:
    return ApprovalRequest(
        trace_id="trace-gw",
        session_id="sess-gw",
        user=_REQUESTER,
        calls=calls,
        risk=RiskDecision(score=70.0, level="dual_approve", break_down=()),
        permission=PermissionDecision(allowed=True),
        summary="删除销售订单 SO000001",
    )


async def test_gateway_creates_single_action(tmp_path: Path) -> None:
    service = _service(tmp_path)
    gateway = ApprovalGatewayImpl(service, _write_registry())
    call = ToolCallDecision(
        tool_name="kingdee_delete_sales_order", arguments={"bill_no": "SO000001"}
    )

    approval_id = await gateway.request(_request((call,)))

    action = await service.get_for_execution(approval_id)
    assert action.status == "pending"
    assert action.form_id == "SAL_Order"
    assert action.business_key == "SO000001"
    assert action.write_action == "delete"
    assert action.required_approvers == []


async def test_gateway_rejects_multiple_calls(tmp_path: Path) -> None:
    service = _service(tmp_path)
    gateway = ApprovalGatewayImpl(service, _write_registry())
    call = ToolCallDecision(
        tool_name="kingdee_delete_sales_order", arguments={"bill_no": "SO000001"}
    )

    with pytest.raises(ApprovalError) as exc:
        await gateway.request(_request((call, call)))
    assert exc.value.code == "approval_conflict"
    assert list((tmp_path / "approvals").glob("*.json")) == []


async def test_gateway_rejects_non_write_tool(tmp_path: Path) -> None:
    from agent.tools import build_default_registry

    service = _service(tmp_path)
    gateway = ApprovalGatewayImpl(service, build_default_registry())
    call = ToolCallDecision(
        tool_name="kingdee_query_sales_order", arguments={"bill_no": "SO000001"}
    )

    with pytest.raises(ApprovalError) as exc:
        await gateway.request(_request((call,)))
    assert exc.value.code == "approval_conflict"


async def test_gateway_idempotent_by_trace_and_arguments(tmp_path: Path) -> None:
    service = _service(tmp_path)
    gateway = ApprovalGatewayImpl(service, _write_registry())
    call = ToolCallDecision(
        tool_name="kingdee_delete_sales_order", arguments={"bill_no": "SO000001"}
    )

    first = await gateway.request(_request((call,)))
    second = await gateway.request(_request((call,)))

    assert first == second
    assert len(list((tmp_path / "approvals").glob("*.json"))) == 1
