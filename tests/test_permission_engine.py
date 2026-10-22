"""M4-T08：四层权限与数据域隔离单测。

覆盖场景 7（operator 越权查财务被拦截）、工具列表过滤、行级/列级判定、
写规则升级/拒绝、PermissionGuard 管道集成与配置驱动。
"""

import json
from typing import Any, cast

import pytest
from pydantic import Field

from agent.tools import ToolDefinition, ToolRegistry, ToolType, build_default_registry
from config.permission_rules import DataDomain, WriteRule
from erp import FilterExpression, eq
from kingdee.endpoints.base import EndpointDescriptor, EndpointParams, FieldDescriptor
from models.base import KingPilotBaseModel, Role
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.permission_engine import (
    PermissionEngine,
    PermissionGuard,
    apply_column_filter,
)


class _Params(EndpointParams):
    amount: float = Field(default=0.0, description="金额（元）")


def _empty_filter(params: KingPilotBaseModel) -> FilterExpression:
    return FilterExpression()


def _endpoint(form_id: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        form_id=form_id,
        label=form_id,
        fields=(
            FieldDescriptor(
                name="FBillNo", data_type="string", filterable=True, description="编号"
            ),
        ),
        default_field_keys=("FBillNo",),
        default_sort="FBillNo",
    )


def _tool(
    name: str,
    form_id: str,
    *,
    tool_type: ToolType = "query",
    action: str = "",
) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name=name,
            description="测试工具",
            parameters=_Params.model_json_schema(),
        ),
        params_model=_Params,
        build_filter=_empty_filter,
        endpoint=_endpoint(form_id),
        tool_type=tool_type,
        action=action,
    )


def _user(role: Role = "operator", **scope: Any) -> UserContext:
    return UserContext(user_id="u001", role=role, data_scope=dict(scope))


def _context(tool_name: str, user: UserContext) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments={},
        user_context=user,
        trace_id="trace-permission",
    )


_FINANCE_TOOL = _tool("erp_query_finance_vouchers", "GL_Voucher")
_HR_TOOL = _tool("erp_query_employees", "BAS_Employee")
_SALES_WRITE = _tool("erp_create_sales_order", "SAL_Order", tool_type="write", action="save")
_PURCHASE_WRITE = _tool(
    "erp_create_purchase_order",
    "PUR_PurchaseOrder",
    tool_type="write",
    action="save",
)
_PRODUCTION_DELETE = _tool(
    "erp_delete_production_order",
    "PRD_Order",
    tool_type="write",
    action="delete",
)
_PRODUCTION_SAVE = _tool(
    "erp_create_production_order",
    "PRD_Order",
    tool_type="write",
    action="save",
)


def _sales_query() -> ToolDefinition:
    return build_default_registry().get("erp_query_sales_order")


# ── 场景 7：越权查询拦截 ──


def test_operator_finance_query_denied() -> None:
    decision = PermissionEngine().check(_user("operator"), _FINANCE_TOOL, {})

    assert decision.allowed is False
    assert "财务数据的访问权限" in decision.reason
    assert decision.domain == DataDomain.FINANCE
    assert decision.access_type == "read"
    payload = json.loads(json.dumps(decision.to_dict()))
    assert payload["domain"] == "finance"
    assert payload["allowed"] is False


def test_domain_filter_excludes_finance_for_operator() -> None:
    engine = PermissionEngine()
    tools = [*build_default_registry().all_tools(), _FINANCE_TOOL]

    names = [spec.name for spec in engine.domain_filter(_user("operator"), tools)]

    assert "erp_query_finance_vouchers" not in names
    assert "erp_query_sales_order" in names
    assert "erp_query_purchase_order" in names
    assert "erp_query_inventory" in names


def test_admin_sees_all_domains() -> None:
    engine = PermissionEngine()
    tools = [*build_default_registry().all_tools(), _FINANCE_TOOL]

    names = [spec.name for spec in engine.domain_filter(_user("admin"), tools)]

    assert "erp_query_finance_vouchers" in names


# ── 工具层与未知配置 ──


def test_domain_filter_respects_tool_lists_and_unknowns() -> None:
    engine = PermissionEngine()
    denied_user = UserContext(user_id="u001", role="operator", deny_tools=["erp_query_sales_order"])
    allow_user = UserContext(user_id="u001", role="operator", allow_tools=["erp_query_customer"])
    ghost = UserContext.model_construct(user_id="u001", role=cast(Role, "ghost"))
    unknown = _tool("erp_query_unknown", "UNKNOWN_FORM")
    sales = _sales_query()
    customer = build_default_registry().get("erp_query_customer")

    denied_names = [spec.name for spec in engine.domain_filter(denied_user, [sales])]
    allowed_names = [spec.name for spec in engine.domain_filter(allow_user, [sales, customer])]
    ghost_names = [spec.name for spec in engine.domain_filter(ghost, [sales])]
    unknown_names = [spec.name for spec in engine.domain_filter(_user("operator"), [unknown])]

    assert denied_names == []
    assert allowed_names == ["erp_query_customer"]
    assert ghost_names == []
    assert unknown_names == []


def test_unknown_role_denied() -> None:
    ghost = UserContext.model_construct(user_id="u001", role=cast(Role, "ghost"))

    decision = PermissionEngine().check(ghost, _sales_query(), {})

    assert decision.allowed is False
    assert "未知角色" in decision.reason


def test_unknown_form_denied() -> None:
    tool = _tool("erp_query_unknown", "UNKNOWN_FORM")

    decision = PermissionEngine().check(_user("operator"), tool, {})

    assert decision.allowed is False
    assert "未知表单" in decision.reason


def test_deny_tools_takes_precedence() -> None:
    user = UserContext(user_id="u001", role="operator", deny_tools=["erp_query_sales_order"])

    decision = PermissionEngine().check(user, _sales_query(), {})

    assert decision.allowed is False
    assert "显式禁用" in decision.reason


def test_allow_tools_whitelist() -> None:
    user = UserContext(user_id="u001", role="operator", allow_tools=["erp_query_customer"])

    denied = PermissionEngine().check(user, _sales_query(), {})
    allowed = PermissionEngine().check(user, build_default_registry().get("erp_query_customer"), {})

    assert denied.allowed is False
    assert "允许列表" in denied.reason
    assert allowed.allowed is True


# ── 数据域读写 ──


def test_salesperson_can_write_sales_but_not_purchase() -> None:
    engine = PermissionEngine()

    allowed = engine.check(_user("salesperson"), _SALES_WRITE, {"amount": 50_000})
    denied = engine.check(_user("salesperson"), _PURCHASE_WRITE, {"amount": 50_000})

    assert allowed.allowed is True
    assert denied.allowed is False
    assert "采购数据的访问权限" in denied.reason


def test_operator_has_no_writable_domain() -> None:
    decision = PermissionEngine().check(_user("operator"), _SALES_WRITE, {})

    assert decision.allowed is False
    assert "销售数据的访问权限" in decision.reason
    assert decision.access_type == "write"


# ── 行级与数据范围 ──


def test_row_filter_single_org() -> None:
    decision = PermissionEngine().check(_user("operator", org_ids=["ORG001"]), _sales_query(), {})

    assert decision.allowed is True
    assert decision.row_filter == FilterExpression(conditions=(eq("FSaleOrgId.FNumber", "ORG001"),))


def test_row_filter_owner_only() -> None:
    decision = PermissionEngine().check(_user("operator", owner_only=True), _sales_query(), {})

    assert decision.allowed is True
    assert decision.row_filter == FilterExpression(conditions=(eq("FSaleEmpId", "u001"),))


def test_row_filter_combines_org_and_owner() -> None:
    decision = PermissionEngine().check(
        _user("operator", org_ids=["ORG001"], owner_only=True), _sales_query(), {}
    )

    assert decision.row_filter == FilterExpression(
        conditions=(eq("FSaleOrgId.FNumber", "ORG001"), eq("FSaleEmpId", "u001"))
    )


def test_multi_org_row_filter_denied() -> None:
    decision = PermissionEngine().check(
        _user("operator", org_ids=["ORG001", "ORG002"]), _sales_query(), {}
    )

    assert decision.allowed is False
    assert "多组织" in decision.reason


def test_row_filter_requires_field_mapping() -> None:
    customer = build_default_registry().get("erp_query_customer")

    decision = PermissionEngine().check(_user("operator", org_ids=["ORG001"]), customer, {})

    assert decision.allowed is False
    assert "未配置组织级行过滤字段" in decision.reason


def test_owner_filter_requires_field_mapping() -> None:
    customer = build_default_registry().get("erp_query_customer")

    decision = PermissionEngine().check(_user("operator", owner_only=True), customer, {})

    assert decision.allowed is False
    assert "未配置负责人行过滤字段" in decision.reason


def test_illegal_user_id_denied() -> None:
    user = UserContext(user_id="u'001", role="operator", data_scope={"owner_only": True})

    decision = PermissionEngine().check(user, _sales_query(), {})

    assert decision.allowed is False
    assert "非法字符" in decision.reason


def test_illegal_org_identifier_denied() -> None:
    decision = PermissionEngine().check(_user("operator", org_ids=["ORG'1"]), _sales_query(), {})

    assert decision.allowed is False
    assert "非法字符" in decision.reason


def test_form_whitelist_denied() -> None:
    customer = build_default_registry().get("erp_query_customer")

    decision = PermissionEngine().check(_user("operator", form_ids=["SAL_Order"]), customer, {})

    assert decision.allowed is False
    assert "不在您的数据范围" in decision.reason


# ── 列级权限 ──


def test_column_deny_for_finance_staff() -> None:
    decision = PermissionEngine().check(_user("finance_staff"), _FINANCE_TOOL, {})

    assert decision.allowed is True
    assert decision.denied_fields == ("FBankAccount",)


def test_column_deny_for_hr_manager() -> None:
    decision = PermissionEngine().check(_user("manager"), _HR_TOOL, {})

    assert decision.allowed is True
    assert decision.denied_fields == ("FIdCard", "FSalary")


def test_apply_column_filter_strips_fields() -> None:
    rows = [{"FBillNo": "V001", "FBankAccount": "6222****"}, {"FBillNo": "V002"}]

    filtered = apply_column_filter(rows, ("FBankAccount",))

    assert filtered == [{"FBillNo": "V001"}, {"FBillNo": "V002"}]
    assert apply_column_filter(rows, ()) == rows


def test_column_deny_merges_data_scope() -> None:
    decision = PermissionEngine().check(
        _user("operator", denied_fields=["FNote"]), _sales_query(), {}
    )

    assert decision.denied_fields == ("FNote",)


# ── 写规则 ──


def test_salesperson_amount_under_limit_passes() -> None:
    decision = PermissionEngine().check(_user("salesperson"), _SALES_WRITE, {"amount": 50_000})

    assert decision.allowed is True
    assert decision.required_approvers == ()
    assert decision.rule_id == ""


def test_salesperson_amount_over_limit_escalates() -> None:
    decision = PermissionEngine().check(_user("salesperson"), _SALES_WRITE, {"amount": 150_000})

    assert decision.allowed is True
    assert decision.required_approvers == ("sales_manager",)
    assert decision.rule_id == "SAL_AMOUNT_LIMIT"
    assert "升级审批" in decision.reason


def test_production_delete_denied() -> None:
    decision = PermissionEngine().check(_user("manager"), _PRODUCTION_DELETE, {"amount": 0})

    assert decision.allowed is False
    assert decision.rule_id == "NO_DELETE_PRODUCTION"
    assert "生产单据禁止删除" in decision.reason


def test_production_save_allowed() -> None:
    decision = PermissionEngine().check(_user("manager"), _PRODUCTION_SAVE, {"amount": 0})

    assert decision.allowed is True


def test_write_rules_not_applied_to_reads() -> None:
    decision = PermissionEngine().check(_user("salesperson"), _sales_query(), {"amount": 1_000_000})

    assert decision.allowed is True
    assert decision.required_approvers == ()


def test_write_rule_injection_is_config_driven() -> None:
    engine = PermissionEngine(
        write_rules=(
            WriteRule(
                rule_id="CUSTOM_DENY",
                label="自定义禁止规则",
                form_ids=("SAL_Order",),
            ),
        )
    )

    decision = engine.check(_user("salesperson"), _SALES_WRITE, {"amount": 1})

    assert decision.allowed is False
    assert decision.rule_id == "CUSTOM_DENY"


@pytest.mark.parametrize(
    ("amount", "escalated"),
    [("150000", True), ("abc", False), (True, False), ([], False)],
)
def test_write_rule_amount_parsing(amount: Any, escalated: bool) -> None:
    decision = PermissionEngine().check(_user("salesperson"), _SALES_WRITE, {"amount": amount})

    assert decision.allowed is True
    assert bool(decision.required_approvers) is escalated


# ── 配置缺失与管道集成 ──


def test_empty_matrix_or_form_domains_rejected() -> None:
    with pytest.raises(ValueError, match="矩阵不能为空"):
        PermissionEngine(matrix={})
    with pytest.raises(ValueError, match="域映射不能为空"):
        PermissionEngine(form_domains={})


async def test_permission_guard_blocks_in_pipeline() -> None:
    registry = ToolRegistry()
    registry.register(_FINANCE_TOOL)
    pipeline = GuardPipeline([PermissionGuard(registry, PermissionEngine())])

    result = await pipeline.check(_context(_FINANCE_TOOL.name, _user("operator")))

    assert result.blocked is True
    assert result.blocked_by == "guard_permission"
    assert result.reason.startswith("权限不足：")
    assert result.records[0].detail["domain"] == "finance"


async def test_permission_guard_allows_visible_domain() -> None:
    registry = ToolRegistry()
    registry.register(_FINANCE_TOOL)
    pipeline = GuardPipeline([PermissionGuard(registry, PermissionEngine())])

    result = await pipeline.check(_context(_FINANCE_TOOL.name, _user("finance_staff")))

    assert result.blocked is False
    assert result.records[0].detail["denied_fields"] == ["FBankAccount"]


async def test_permission_guard_unknown_tool_blocked() -> None:
    pipeline = GuardPipeline([PermissionGuard(ToolRegistry(), PermissionEngine())])

    result = await pipeline.check(_context("kingdee_not_exists", _user("operator")))

    assert result.blocked is True
    assert "未注册" in result.reason
