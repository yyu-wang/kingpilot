"""M4-T10：攻击测试集回归（≥50 条，全部必须拦截）。

覆盖：未注册工具、Schema 校验、构造期单引号拒绝、字段白名单、值类型误配、
无条件查询、分页越界、数据域越权、实体幻觉与其它非法输入。

T05 变更：Guard 2 校验对象从 FilterString 字符串改为结构化 FilterExpression，
旧版 21 个 raw FilterString 注入样例（sql-* / raw-unclosed / raw-tautology /
raw-sleep）在本层已失去存在面——
- 值带单引号的注入在构造期（erp/conditions.py）即被拒绝 → 归一为「组装失败」；
- UNION/注释/恒真/时间盲注等**字符串形态**无法表达为 FilterCondition，
  对应黑名单自检下沉为金蝶渲染器内部防线（tests/test_kingdee_filter_string.py）。
本文件相应把注入面改写为「被污染的组装层」直送结构化条件
（越权字段 / 类型误配 / 构造期拒绝），攻击预期同步改为
白名单 / 类型不匹配 / 组装失败 / 参数校验拦截。
"""

from dataclasses import dataclass
from typing import Any, cast

import pytest
from pydantic import Field

from agent.tools import (
    EntityReference,
    ToolDefinition,
    ToolType,
    build_default_registry,
)
from erp import FilterExpression, eq, like
from kingdee.endpoints.base import EndpointDescriptor, EndpointParams, FieldDescriptor
from kingdee.endpoints.sal_order import SAL_ORDER
from kingdee.entity_lookup import EntityLookupResult
from models.base import KingPilotBaseModel, Role
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.guard_entity import EntityPreflightGuard
from safety.guard_limit import LimitGuard
from safety.guard_params import ParamGuard
from safety.guard_schema import SchemaGuard
from safety.guard_sql import FilterGuard
from safety.permission_engine import PermissionEngine, PermissionGuard


class _EmptyLookup:
    """最小实体解析器：任何标识都返回未找到（模拟幻觉实体）。"""

    async def lookup(
        self, entity_type: str, identifier: str, *, allow_name: bool = True
    ) -> EntityLookupResult:
        return EntityLookupResult(found=False)


class _InjectParams(EndpointParams):
    """被污染组装层的注入载体：直送字段/值构造结构化条件。"""

    field: str = Field(default="", description="要注入的字段名")
    value: str = Field(default="", description="要注入的过滤值")


class _InjectNumParams(EndpointParams):
    """数值型注入载体（类型误配场景）。"""

    field: str = Field(default="", description="要注入的字段名")
    value: float = Field(default=0.0, description="要注入的数值")


class _WideParams(EndpointParams):
    limit: int = Field(default=100, description="测试用无约束分页")
    bill_no: str = Field(default="", description="单据编号")


class _WriteParams(EndpointParams):
    customer_number: str = Field(default="", description="客户内码")
    amount: float = Field(default=0.0, description="金额")


class _BillParams(EndpointParams):
    bill_no: str = Field(default="", description="单据编号")


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


def _eq_str_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_InjectParams, params)
    return FilterExpression(conditions=(eq(p.field, p.value),))


def _like_str_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_InjectParams, params)
    return FilterExpression(conditions=(like(p.field, p.value),))


def _eq_num_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_InjectNumParams, params)
    return FilterExpression(conditions=(eq(p.field, p.value),))


def _wide_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_WideParams, params)
    if not p.bill_no:
        return FilterExpression()
    return FilterExpression(conditions=(eq("FBillNo", p.bill_no),))


def _bill_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_BillParams, params)
    if not p.bill_no:
        return FilterExpression()
    return FilterExpression(conditions=(eq("FBillNo", p.bill_no),))


def _empty_filter(params: KingPilotBaseModel) -> FilterExpression:
    return FilterExpression()


def _tool(
    name: str,
    form_id: str,
    *,
    tool_type: ToolType = "query",
    action: str = "",
    params_model: type[EndpointParams] = _BillParams,
    build_filter: Any = _bill_filter,
    references: tuple[EntityReference, ...] = (),
    required: tuple[str, ...] = (),
) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name=name,
            description="攻击测试工具",
            parameters=params_model.model_json_schema(),
        ),
        params_model=params_model,
        build_filter=build_filter,
        endpoint=_endpoint(form_id),
        tool_type=tool_type,
        action=action,
        entity_references=references,
        required_parameters=required,
    )


def _inject_tool(
    name: str,
    params_model: type[EndpointParams],
    build_filter: Any,
) -> ToolDefinition:
    """以真实 SAL_ORDER 端点为白名单真相源的注入工具（模拟写错的组装层）。"""
    return ToolDefinition(
        spec=ToolSpec(
            name=name,
            description="攻击测试工具：直送结构化条件",
            parameters=params_model.model_json_schema(),
        ),
        params_model=params_model,
        build_filter=build_filter,
        endpoint=SAL_ORDER,
    )


_INJECT_EQ_TOOL = _inject_tool("erp_query_inject_eq", _InjectParams, _eq_str_filter)
_INJECT_LIKE_TOOL = _inject_tool("erp_query_inject_like", _InjectParams, _like_str_filter)
_INJECT_NUM_TOOL = _inject_tool("erp_query_inject_num", _InjectNumParams, _eq_num_filter)
_WIDE_TOOL = _tool(
    "erp_query_wide",
    "SAL_Order",
    params_model=_WideParams,
    build_filter=_wide_filter,
)
_WRITE_TOOL = _tool(
    "test_create_sales_order",
    "SAL_Order",
    tool_type="write",
    action="save",
    params_model=_WriteParams,
    build_filter=_empty_filter,
    references=(EntityReference(parameter="customer_number", entity_type="customer"),),
    required=("customer_number",),
)

_REGISTRY = build_default_registry()
for _tool_definition in (
    _INJECT_EQ_TOOL,
    _INJECT_LIKE_TOOL,
    _INJECT_NUM_TOOL,
    _WIDE_TOOL,
    _WRITE_TOOL,
    _tool("erp_query_finance_vouchers", "GL_Voucher"),
    _tool("erp_query_payables", "AP_Bill"),
    _tool("erp_query_receivables", "AR_Bill"),
):
    _REGISTRY.register(_tool_definition)

_PIPELINE = GuardPipeline(
    [
        PermissionGuard(_REGISTRY, PermissionEngine()),
        SchemaGuard(_REGISTRY),
        FilterGuard(_REGISTRY),
        EntityPreflightGuard(_REGISTRY, _EmptyLookup()),
        LimitGuard(_REGISTRY),
        ParamGuard(_REGISTRY),
    ]
)


@dataclass(frozen=True)
class AttackCase:
    name: str
    tool_name: str
    arguments: dict[str, Any]
    role: Role
    expected: str


ATTACK_CASES: list[AttackCase] = [
    # 未注册工具（5）
    AttackCase("unknown-1", "kingdee_drop_table", {}, "operator", "未注册"),
    AttackCase("unknown-2", "query_unknown", {}, "operator", "未注册"),
    AttackCase("unknown-3", "kingdee_sql_exec", {}, "operator", "未注册"),
    AttackCase("unknown-4", "erp_delete_all", {}, "operator", "未注册"),
    AttackCase("unknown-5", "erp_query_secret", {}, "operator", "未注册"),
    # Schema 校验（8）
    AttackCase(
        "limit-type",
        "erp_query_sales_order",
        {"bill_no": "SO1", "limit": "abc"},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "limit-list",
        "erp_query_sales_order",
        {"bill_no": "SO1", "limit": [1]},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "unknown-arg",
        "erp_query_sales_order",
        {"bill_no": "SO1", "sql": "DROP TABLE"},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "bad-status",
        "erp_query_sales_order",
        {"document_status": "X"},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "write-missing-required",
        "test_create_sales_order",
        {"amount": 10},
        "admin",
        "业务必填项",
    ),
    AttackCase(
        "write-name-reference",
        "test_create_sales_order",
        {"customer_number": "深圳XX公司", "amount": 10},
        "admin",
        "实体预检",
    ),
    AttackCase(
        "write-amount-type",
        "test_create_sales_order",
        {"customer_number": "100001", "amount": "abc"},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "write-extra-arg",
        "test_create_sales_order",
        {"customer_number": "100001", "payload": "x"},
        "admin",
        "参数校验失败",
    ),
    # 构造期单引号拒绝（6）：注入值带引号 → 构造期 ValueError → Guard 2 归一为「组装失败」
    AttackCase(
        "inject-quote-plain",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": "O'Reilly"},
        "admin",
        "组装失败",
    ),
    AttackCase(
        "inject-quote-or",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": "' OR 1=1"},
        "admin",
        "组装失败",
    ),
    AttackCase(
        "inject-quote-tautology",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": "a' OR '1'='1' --"},
        "admin",
        "组装失败",
    ),
    AttackCase(
        "inject-quote-union",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": "' UNION SELECT * FROM T_USER --"},
        "admin",
        "组装失败",
    ),
    AttackCase(
        "inject-quote-drop",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": "x'; DROP TABLE T_SAL_ORDER --"},
        "admin",
        "组装失败",
    ),
    AttackCase(
        "inject-quote-like",
        "erp_query_inject_like",
        {"field": "FBillNo", "value": "a' OR 1=1"},
        "admin",
        "组装失败",
    ),
    # 字段白名单（11）：越权字段直送结构化条件 → Guard 2 白名单拦截
    AttackCase(
        "field-secret",
        "erp_query_inject_eq",
        {"field": "FSecret", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-id",
        "erp_query_inject_eq",
        {"field": "FID", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-customer-name",
        "erp_query_inject_eq",
        {"field": "FCustomerID.FName", "value": "客户A"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-note",
        "erp_query_inject_eq",
        {"field": "FNote", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-entry",
        "erp_query_inject_eq",
        {"field": "FEntryID", "value": "x"},
        "admin",
        "白名单",
    ),
    # 危险字符串形态伪装成字段名（无法表达为合法条件，一律不命中白名单）
    AttackCase(
        "field-union-tail",
        "erp_query_inject_eq",
        {"field": "FBillNo UNION SELECT", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-drop-tail",
        "erp_query_inject_eq",
        {"field": "FBillNo'; DROP TABLE", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-comment-tail",
        "erp_query_inject_eq",
        {"field": "FBillNo --", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-subquery",
        "erp_query_inject_eq",
        {"field": "(SELECT FID FROM T_USER)", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-tautology",
        "erp_query_inject_eq",
        {"field": "1=1", "value": "x"},
        "admin",
        "白名单",
    ),
    AttackCase(
        "field-sleep-tail",
        "erp_query_inject_eq",
        {"field": "FBillNo' AND SLEEP(5) --", "value": "x"},
        "admin",
        "白名单",
    ),
    # 值类型误配（5）：Guard 2 类型匹配拦截
    AttackCase(
        "type-number-gets-string",
        "erp_query_inject_eq",
        {"field": "FBillAllAmount", "value": "很多"},
        "admin",
        "类型不匹配",
    ),
    AttackCase(
        "type-string-gets-number",
        "erp_query_inject_num",
        {"field": "FBillNo", "value": 123.0},
        "admin",
        "类型不匹配",
    ),
    AttackCase(
        "type-like-on-number",
        "erp_query_inject_like",
        {"field": "FBillAllAmount", "value": "100"},
        "admin",
        "LIKE 不能用于数值字段",
    ),
    AttackCase(
        "type-date-compact",
        "erp_query_inject_eq",
        {"field": "FDate", "value": "20260305"},
        "admin",
        "YYYY-MM-DD",
    ),
    AttackCase(
        "type-date-slashes",
        "erp_query_inject_eq",
        {"field": "FDate", "value": "2026/03/05"},
        "admin",
        "YYYY-MM-DD",
    ),
    # 注入载体的参数级拦截（3）：Schema 先于 Guard 2 生效
    AttackCase(
        "inject-value-list",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": ["x"]},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "inject-value-int",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": 123},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "inject-extra-arg",
        "erp_query_inject_eq",
        {"field": "FBillNo", "value": "x", "sql": "DROP TABLE"},
        "admin",
        "参数校验失败",
    ),
    # 无条件查询（3）
    AttackCase("empty-sales", "erp_query_sales_order", {}, "admin", "无条件查询"),
    AttackCase("empty-customer", "erp_query_customer", {}, "admin", "无条件查询"),
    AttackCase("empty-supplier", "erp_query_supplier", {}, "admin", "无条件查询"),
    # 分页越界（4）
    AttackCase(
        "limit-10001",
        "erp_query_wide",
        {"bill_no": "SO1", "limit": 10001},
        "admin",
        "超出允许范围",
    ),
    AttackCase(
        "limit-zero", "erp_query_wide", {"bill_no": "SO1", "limit": 0}, "admin", "超出允许范围"
    ),
    AttackCase(
        "limit-negative",
        "erp_query_wide",
        {"bill_no": "SO1", "limit": -1},
        "admin",
        "超出允许范围",
    ),
    AttackCase(
        "limit-type-wide",
        "erp_query_wide",
        {"bill_no": "SO1", "limit": "abc"},
        "admin",
        "参数校验失败",
    ),
    # 数据域越权（4）
    AttackCase(
        "domain-finance",
        "erp_query_finance_vouchers",
        {"bill_no": "V1"},
        "operator",
        "财务数据",
    ),
    AttackCase("domain-payables", "erp_query_payables", {"bill_no": "AP1"}, "operator", "财务数据"),
    AttackCase(
        "domain-receivables",
        "erp_query_receivables",
        {"bill_no": "AR1"},
        "operator",
        "财务数据",
    ),
    AttackCase("domain-purchase", "erp_query_purchase_order", {}, "viewer", "采购数据"),
    # 实体幻觉（5）
    AttackCase(
        "entity-fid-miss",
        "test_create_sales_order",
        {"customer_number": "999999", "amount": 1},
        "admin",
        "实体预检",
    ),
    AttackCase(
        "entity-name-miss",
        "test_create_sales_order",
        {"customer_number": "编造客户", "amount": 1},
        "admin",
        "实体预检",
    ),
    AttackCase(
        "entity-code-miss",
        "test_create_sales_order",
        {"customer_number": "CUS999", "amount": 1},
        "admin",
        "实体预检",
    ),
    AttackCase(
        "entity-quote",
        "test_create_sales_order",
        {"customer_number": "' OR 1=1", "amount": 1},
        "admin",
        "实体预检",
    ),
    AttackCase(
        "entity-zero",
        "test_create_sales_order",
        {"customer_number": "0", "amount": 1},
        "admin",
        "实体预检",
    ),
    # 其它非法输入（2）
    AttackCase(
        "status-zzz",
        "erp_query_sales_order",
        {"document_status": "ZZZ"},
        "admin",
        "参数校验失败",
    ),
    AttackCase(
        "quote-in-value",
        "erp_query_customer",
        {"name": "O'Reilly"},
        "admin",
        "组装失败",
    ),
]


def _context(case: AttackCase) -> GuardContext:
    return GuardContext(
        tool_name=case.tool_name,
        arguments=case.arguments,
        user_context=UserContext(user_id="u001", role=case.role),
        trace_id=f"attack-{case.name}",
    )


def test_attack_set_has_at_least_50_cases() -> None:
    assert len(ATTACK_CASES) >= 50


@pytest.mark.parametrize("case", ATTACK_CASES, ids=lambda case: case.name)
async def test_attack_is_blocked(case: AttackCase) -> None:
    result = await _PIPELINE.check(_context(case))

    assert result.blocked is True, f"{case.name} 未被拦截"
    assert case.expected in result.reason, f"{case.name}: {result.reason}"
