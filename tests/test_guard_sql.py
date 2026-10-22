"""M4-T03：Guard 2（FilterExpression 校验）单测（T05 重写）。

覆盖验收点：
- 正常查询放行（字段清单断言）；
- 字段白名单拦截（越权字段 / 可查不可过滤字段）；
- 值类型匹配拦截（数值字段收字符串 / LIKE 用于数值字段 / 文本字段收数值 / 日期形状）；
- 未知工具 / 参数非法 / 组装层拒绝（单引号）fail-closed；
- 空表达式放行（无条件拒绝归 Guard 4）与管道集成。

T05 变更：校验对象从 FilterString 字符串改为结构化 ``FilterExpression``——
旧版 18 个注入样例（UNION/注释/恒真/时间盲注等）在本层已无法存在：
单引号在构造期（erp/conditions.py）即被拒绝，危险模式黑名单下沉为金蝶
渲染器内部自检（覆盖于 tests/test_kingdee_filter_string.py）。
"""

from collections.abc import Callable
from typing import Any, cast

import pytest
from pydantic import Field

from agent.tools import ToolDefinition, ToolRegistry, build_default_registry
from erp import FilterExpression, eq, like
from kingdee.endpoints.base import EndpointParams
from kingdee.endpoints.sal_order import SAL_ORDER
from models.base import KingPilotBaseModel
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.guard_sql import FilterGuard


class _InjectParams(EndpointParams):
    """测试用参数模型：携带要注入的字段/值（模拟写错或被污染的组装层）。"""

    field: str = Field(default="", description="要注入的字段名")
    value: str = Field(default="", description="要注入的过滤值")


class _InjectNumParams(EndpointParams):
    """测试用参数模型：携带数值型注入值（类型误配场景）。"""

    field: str = Field(default="", description="要注入的字段名")
    value: float = Field(default=0.0, description="要注入的数值")


def _eq_str_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_InjectParams, params)
    return FilterExpression(conditions=(eq(p.field, p.value),))


def _like_str_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_InjectParams, params)
    return FilterExpression(conditions=(like(p.field, p.value),))


def _eq_num_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_InjectNumParams, params)
    return FilterExpression(conditions=(eq(p.field, p.value),))


def _make_tool(
    name: str,
    params_model: type[KingPilotBaseModel],
    build_filter: Callable[[KingPilotBaseModel], FilterExpression],
) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name=name,
            description="测试用：模拟组装出越权/类型错误条件的工具",
            parameters=params_model.model_json_schema(),
        ),
        params_model=params_model,
        build_filter=build_filter,
        endpoint=SAL_ORDER,
    )


_EQ_TOOL = _make_tool("erp_query_inject_eq", _InjectParams, _eq_str_filter)
_LIKE_TOOL = _make_tool("erp_query_inject_like", _InjectParams, _like_str_filter)
_NUM_TOOL = _make_tool("erp_query_inject_num", _InjectNumParams, _eq_num_filter)


def _registry_with(*tools: ToolDefinition) -> ToolRegistry:
    registry = build_default_registry()
    for tool in tools:
        registry.register(tool)
    return registry


def _context(tool_name: str, arguments: dict[str, Any]) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments=arguments,
        user_context=UserContext(user_id="u001", role="viewer"),
        trace_id="trace-filter",
    )


# ── 正常放行 ──


async def test_valid_equality_and_numeric_conditions_pass() -> None:
    result = await FilterGuard(build_default_registry()).check(
        _context("erp_query_sales_order", {"bill_no": "SO000008", "min_amount": 1000.5})
    )

    assert result.passed is True
    assert result.detail["condition_count"] == 2
    assert result.detail["fields"] == ["FBillAllAmount", "FBillNo"]


async def test_like_condition_passes() -> None:
    result = await FilterGuard(build_default_registry()).check(
        _context("erp_query_customer", {"name": "测试"})
    )

    assert result.passed is True
    assert result.detail["fields"] == ["FName"]


async def test_keyword_like_values_are_not_false_positive() -> None:
    """值里含 SQL 关键字字样只是普通文本——本层不做字符串黑名单，不误杀。"""
    result = await FilterGuard(build_default_registry()).check(
        _context("erp_query_customer", {"name": "DROP TABLE 客户"})
    )

    assert result.passed is True


async def test_empty_filter_passes_by_design() -> None:
    """空条件拒绝归 Guard 4：本 Guard 只校验「有条件的查询是否安全」。"""
    result = await FilterGuard(build_default_registry()).check(
        _context("erp_query_sales_order", {})
    )

    assert result.passed is True
    assert result.detail["filter_empty"] is True


# ── 字段白名单 ──


@pytest.mark.parametrize(
    "field",
    [
        "FSecret",  # 未知字段
        "FID",  # 可查不可过滤
        "FCustomerID.FName",  # 显示字段不可过滤
    ],
)
async def test_field_whitelist_blocks(field: str) -> None:
    result = await FilterGuard(_registry_with(_EQ_TOOL)).check(
        _context("erp_query_inject_eq", {"field": field, "value": "x"})
    )

    assert result.passed is False
    assert "白名单" in result.reason
    assert result.detail["field"] == field
    assert "FBillNo" in result.detail["allowed_fields"]


# ── 值类型匹配 ──


async def test_number_field_rejects_string_value() -> None:
    result = await FilterGuard(_registry_with(_EQ_TOOL)).check(
        _context("erp_query_inject_eq", {"field": "FBillAllAmount", "value": "很多"})
    )

    assert result.passed is False
    assert "类型不匹配" in result.reason
    assert result.detail["field"] == "FBillAllAmount"
    assert result.detail["data_type"] == "number"


async def test_number_field_accepts_numeric_value() -> None:
    result = await FilterGuard(_registry_with(_NUM_TOOL)).check(
        _context("erp_query_inject_num", {"field": "FBillAllAmount", "value": 100.5})
    )

    assert result.passed is True


async def test_like_rejects_number_field() -> None:
    result = await FilterGuard(_registry_with(_LIKE_TOOL)).check(
        _context("erp_query_inject_like", {"field": "FBillAllAmount", "value": "100"})
    )

    assert result.passed is False
    assert "LIKE 不能用于数值字段" in result.reason


async def test_string_field_rejects_numeric_value() -> None:
    result = await FilterGuard(_registry_with(_NUM_TOOL)).check(
        _context("erp_query_inject_num", {"field": "FBillNo", "value": 123.0})
    )

    assert result.passed is False
    assert "类型不匹配" in result.reason
    assert result.detail["data_type"] == "string"


async def test_date_field_rejects_malformed_value() -> None:
    result = await FilterGuard(_registry_with(_EQ_TOOL)).check(
        _context("erp_query_inject_eq", {"field": "FDate", "value": "20260305"})
    )

    assert result.passed is False
    assert "YYYY-MM-DD" in result.reason


async def test_date_field_accepts_wellformed_value() -> None:
    result = await FilterGuard(build_default_registry()).check(
        _context("erp_query_sales_order", {"order_date": "2026-03-05"})
    )

    assert result.passed is True
    assert result.detail["fields"] == ["FDate"]


# ── 其它 fail-closed 分支 ──


async def test_unknown_tool_blocked() -> None:
    result = await FilterGuard(build_default_registry()).check(_context("kingdee_drop_table", {}))

    assert result.passed is False
    assert "未注册" in result.reason


async def test_invalid_params_blocked() -> None:
    result = await FilterGuard(build_default_registry()).check(
        _context("erp_query_sales_order", {"bill_no": ["不是字符串"]})
    )

    assert result.passed is False
    assert "参数校验失败" in result.reason
    assert result.detail["error_count"] >= 1


async def test_build_filter_rejection_blocked() -> None:
    """构造期拒绝单引号注入值 → Guard 2 归一为拦截。"""
    result = await FilterGuard(build_default_registry()).check(
        _context("erp_query_sales_order", {"bill_no": "O'Reilly"})
    )

    assert result.passed is False
    assert "组装失败" in result.reason
    assert result.detail["error_type"] == "ValueError"


# ── 管道集成 ──


async def test_guard_works_inside_pipeline() -> None:
    pipeline = GuardPipeline([FilterGuard(_registry_with(_EQ_TOOL))])

    result = await pipeline.check(
        _context("erp_query_inject_eq", {"field": "FSecret", "value": "x"})
    )

    assert result.blocked is True
    assert result.blocked_by == "guard_filter"
    assert result.records[0].detail["field"] == "FSecret"
