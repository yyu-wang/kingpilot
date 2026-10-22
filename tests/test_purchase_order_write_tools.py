"""采购订单写工具（Schema / Guard / 权限 / 风险 / 执行器）。"""

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

_CREATE = "erp_create_purchase_order"
_SUBMIT = "erp_submit_purchase_order"
_AUDIT = "erp_audit_purchase_order"
_UNAUDIT = "erp_unaudit_purchase_order"
_DELETE = "erp_delete_purchase_order"

_CREATE_ARGS = {
    "supplier_number": "SUP001",
    "material_number": "MAT001",
    "quantity": 10,
    "tax_price": 100,
}


def _registry() -> ToolRegistry:
    return build_default_registry()


def _context(tool_name: str, arguments: dict[str, Any], role: str = "purchaser") -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments=arguments,
        user_context=UserContext(user_id="paul", role=role),
        trace_id="trace-purchase-order",
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
        return ActionResult(success=True, form_id=form_id, bill_no=bill_no or "PO900001")


async def test_schema_rejects_unknown_and_missing_parameters() -> None:
    tool = _registry().get(_CREATE)

    with pytest.raises(ToolError, match="参数校验失败"):
        await tool.execute(cast(KingdeeClient, _RecordingClient()), {**_CREATE_ARGS, "bogus": 1})
    with pytest.raises(ToolError, match="参数校验失败"):
        await tool.execute(
            cast(KingdeeClient, _RecordingClient()),
            {"supplier_number": "SUP001", "material_number": "MAT001"},
        )


async def test_schema_rejects_invalid_bill_no() -> None:
    tool = _registry().get(_SUBMIT)

    with pytest.raises(ToolError, match="参数校验失败"):
        await tool.execute(cast(KingdeeClient, _RecordingClient()), {"bill_no": "PO12345"})


async def test_guard2_and_guard4_skip_write_tools() -> None:
    registry = _registry()

    filter_result = await FilterGuard(registry).check(_context(_CREATE, _CREATE_ARGS))
    limit_result = await LimitGuard(registry).check(_context(_CREATE, _CREATE_ARGS))

    assert filter_result.passed is True
    assert filter_result.detail["skipped"] is True
    assert limit_result.passed is True
    assert limit_result.detail["tool_type"] == "write"


async def test_guard5_requires_business_fields() -> None:
    registry = _registry()

    missing = await ParamGuard(registry).check(_context(_CREATE, {"supplier_number": "SUP001"}))
    no_bill = await ParamGuard(registry).check(_context(_SUBMIT, {}))

    assert missing.passed is False
    assert set(missing.detail["missing"]) == {
        "material_number",
        "quantity",
        "tax_price",
    }
    assert no_bill.passed is False
    assert no_bill.detail["missing"] == ["bill_no"]


async def test_permission_roles_for_purchase_order_write() -> None:
    engine = PermissionEngine()
    registry = _registry()
    args = {"bill_no": "PO000004"}

    viewer = engine.check(
        UserContext(user_id="v", role="viewer"), registry.get(_CREATE), _CREATE_ARGS
    )
    sales = engine.check(
        UserContext(user_id="s", role="salesperson"), registry.get(_CREATE), _CREATE_ARGS
    )
    purchaser = engine.check(
        UserContext(user_id="p", role="purchaser"), registry.get(_CREATE), _CREATE_ARGS
    )
    admin = engine.check(UserContext(user_id="a", role="admin"), registry.get(_DELETE), args)

    assert viewer.allowed is False
    assert sales.allowed is False  # 销售角色没有采购写权限
    assert purchaser.allowed is True
    assert admin.allowed is True
    assert purchaser.access_type == "write"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_level"),
    [
        (_CREATE, _CREATE_ARGS, "confirm"),
        (_SUBMIT, {"bill_no": "PO000004"}, "confirm"),
        (_AUDIT, {"bill_no": "PO000003"}, "dual_approve"),
        (_UNAUDIT, {"bill_no": "PO000001"}, "dual_approve"),
        (_DELETE, {"bill_no": "PO000004"}, "dual_approve"),
    ],
)
def test_risk_levels(tool_name: str, arguments: dict[str, Any], expected_level: str) -> None:
    tool = _registry().get(tool_name)
    user = UserContext(user_id="p", role="purchaser")

    decision = RiskEngine().assess(build_risk_context(tool, arguments, user))

    assert decision.level == expected_level


async def test_create_runner_builds_save_model() -> None:
    client = _RecordingClient()
    tool = _registry().get(_CREATE)

    result = await tool.execute(
        cast(KingdeeClient, client),
        {**_CREATE_ARGS, "order_date": "2026-04-01", "note": "加急"},
    )

    assert isinstance(result, ActionResult)
    call = client.calls[0]
    assert call["action"] == "save"
    assert call["form_id"] == "PUR_PurchaseOrder"
    assert call["model"] == {
        "FSupplierID": {"FNumber": "SUP001"},
        "FMaterialID": {"FNumber": "MAT001"},
        "FQty": 10,
        "FTaxPrice": 100,
        "FDate": "2026-04-01",
        "FNote": "加急",
    }


async def test_create_runner_uses_fid_for_rewritten_identifiers() -> None:
    client = _RecordingClient()
    tool = _registry().get(_CREATE)

    await tool.execute(
        cast(KingdeeClient, client),
        {**_CREATE_ARGS, "supplier_number": "4001", "material_number": "3001"},
    )

    model = client.calls[0]["model"]
    assert model["FSupplierID"] == {"FID": 4001}
    assert model["FMaterialID"] == {"FID": 3001}


async def test_action_runner_passes_bill_no_and_form() -> None:
    client = _RecordingClient()
    tool = _registry().get(_DELETE)

    result = await tool.execute(cast(KingdeeClient, client), {"bill_no": "PO000004"})

    assert isinstance(result, ActionResult)
    assert client.calls == [
        {"action": "delete", "form_id": "PUR_PurchaseOrder", "model": None, "bill_no": "PO000004"}
    ]


async def test_guard3_checks_supplier_and_material(sandbox_server: str) -> None:
    async with KingdeeClient(sandbox_settings(sandbox_server)) as client:
        guard = EntityPreflightGuard(_registry(), KingdeeEntityLookup(client))

        action = await guard.check(_context(_SUBMIT, {"bill_no": "PO000004"}))
        create = await guard.check(_context(_CREATE, _CREATE_ARGS))

    assert action.passed is True
    assert action.detail["skipped"] is True
    assert create.passed is True
    assert create.detail["parameter_updates"] == {
        "supplier_number": "4001",
        "material_number": "3001",
    }
