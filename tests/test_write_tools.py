"""M9-T13：销售订单写工具（Schema / Guard / 权限 / 风险 / 执行器）。"""

from typing import Any, cast

import pytest

from agent.tools import ToolRegistry, build_default_registry
from infrastructure.exceptions import ToolError
from kingdee.client import KingdeeClient
from kingdee.entity_lookup import KingdeeEntityLookup
from models.kingdee import ActionResult
from models.user import UserContext
from safety.guard import GuardContext
from safety.guard_entity import EntityPreflightGuard
from safety.guard_limit import LimitGuard
from safety.guard_params import ParamGuard
from safety.guard_sql import FilterGuard
from safety.permission_engine import PermissionEngine
from safety.risk_engine import RiskEngine, build_risk_context
from sandbox import sandbox_settings

_CREATE = "erp_create_sales_order"
_SUBMIT = "erp_submit_sales_order"
_AUDIT = "erp_audit_sales_order"
_UNAUDIT = "erp_unaudit_sales_order"
_DELETE = "erp_delete_sales_order"

_CREATE_ARGS = {
    "customer_number": "CUS001",
    "material_number": "MAT001",
    "quantity": 10,
    "tax_price": 100,
}


def _registry() -> ToolRegistry:
    return build_default_registry()


def _context(tool_name: str, arguments: dict[str, Any], role: str = "salesperson") -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments=arguments,
        user_context=UserContext(user_id="alice", role=role),
        trace_id="trace-write",
    )


class _RecordingClient:
    """记录 invoke_action 调用参数的客户端替身。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def invoke_action(
        self, action: str, form_id: str, *, model: Any = None, bill_no: str = ""
    ) -> ActionResult:
        self.calls.append(
            {"action": action, "form_id": form_id, "model": model, "bill_no": bill_no}
        )
        return ActionResult(success=True, form_id=form_id, bill_no=bill_no or "SO900001")


async def test_schema_rejects_unknown_and_missing_parameters() -> None:
    tool = _registry().get(_CREATE)

    with pytest.raises(ToolError, match="参数校验失败"):
        await tool.execute(cast(KingdeeClient, _RecordingClient()), {**_CREATE_ARGS, "bogus": 1})
    with pytest.raises(ToolError, match="参数校验失败"):
        await tool.execute(cast(KingdeeClient, _RecordingClient()), {"customer_number": "CUS001"})


async def test_guard2_and_guard4_skip_write_tools() -> None:
    registry = _registry()
    arguments = {**_CREATE_ARGS}

    filter_result = await FilterGuard(registry).check(_context(_CREATE, arguments))
    limit_result = await LimitGuard(registry).check(_context(_CREATE, arguments))

    assert filter_result.passed is True
    assert filter_result.detail["skipped"] is True
    assert limit_result.passed is True
    assert limit_result.detail["tool_type"] == "write"


async def test_guard5_requires_business_fields() -> None:
    registry = _registry()

    missing = await ParamGuard(registry).check(
        _context(_CREATE, {"customer_number": "CUS001", "material_number": "MAT001"})
    )
    no_bill = await ParamGuard(registry).check(_context(_SUBMIT, {}))

    assert missing.passed is False
    assert set(missing.detail["missing"]) == {"quantity", "tax_price"}
    assert no_bill.passed is False
    assert no_bill.detail["missing"] == ["bill_no"]


async def test_permission_viewer_denied_salesperson_allowed() -> None:
    engine = PermissionEngine()
    registry = _registry()

    viewer = engine.check(
        UserContext(user_id="v", role="viewer"), registry.get(_CREATE), _CREATE_ARGS
    )
    sales = engine.check(
        UserContext(user_id="a", role="salesperson"), registry.get(_CREATE), _CREATE_ARGS
    )
    manager = engine.check(
        UserContext(user_id="m", role="manager"), registry.get(_DELETE), {"bill_no": "SO000004"}
    )

    assert viewer.allowed is False
    assert sales.allowed is True
    assert manager.allowed is True
    assert manager.access_type == "write"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_level"),
    [
        (_CREATE, _CREATE_ARGS, "confirm"),
        (_SUBMIT, {"bill_no": "SO000004"}, "confirm"),
        (_AUDIT, {"bill_no": "SO000006"}, "dual_approve"),
        (_UNAUDIT, {"bill_no": "SO000001"}, "dual_approve"),
        (_DELETE, {"bill_no": "SO000004"}, "dual_approve"),
    ],
)
def test_risk_levels(tool_name: str, arguments: dict[str, Any], expected_level: str) -> None:
    tool = _registry().get(tool_name)
    user = UserContext(user_id="a", role="salesperson")
    context = build_risk_context(tool, arguments, user)

    decision = RiskEngine().assess(context)

    assert decision.level == expected_level


async def test_create_runner_builds_save_model() -> None:
    client = _RecordingClient()
    tool = _registry().get(_CREATE)

    result = await tool.execute(cast(KingdeeClient, client), {**_CREATE_ARGS, "note": "加急"})

    assert isinstance(result, ActionResult)
    call = client.calls[0]
    assert call["action"] == "save"
    assert call["form_id"] == "SAL_Order"
    assert call["model"]["FCustomerID"] == {"FNumber": "CUS001"}
    assert call["model"]["FMaterialID"] == {"FNumber": "MAT001"}
    assert call["model"]["FQty"] == 10
    assert call["model"]["FTaxPrice"] == 100
    assert call["model"]["FNote"] == "加急"


async def test_action_runner_passes_bill_no() -> None:
    client = _RecordingClient()
    tool = _registry().get(_DELETE)

    result = await tool.execute(cast(KingdeeClient, client), {"bill_no": "SO000004"})

    assert isinstance(result, ActionResult)
    assert client.calls == [
        {"action": "delete", "form_id": "SAL_Order", "model": None, "bill_no": "SO000004"}
    ]


async def test_guard3_skips_action_without_references_but_checks_material(
    sandbox_server: str,
) -> None:
    async with KingdeeClient(sandbox_settings(sandbox_server)) as client:
        guard = EntityPreflightGuard(_registry(), KingdeeEntityLookup(client))

        action = await guard.check(_context(_SUBMIT, {"bill_no": "SO000004"}))
        create = await guard.check(_context(_CREATE, _CREATE_ARGS))

    assert action.passed is True
    assert action.detail["skipped"] is True
    assert create.passed is True
    assert create.detail["parameter_updates"] == {
        "customer_number": "2001",
        "material_number": "3001",
    }
