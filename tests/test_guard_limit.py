"""M4-T05：Guard 4（分页硬限 + 无条件查询）单测。

覆盖验收点：边界值 10000/1001/空条件；外加缺省 limit、非法类型、
写工具豁免、组装失败与「Guard 2 → Guard 4」管道集成。
"""

from typing import Any, cast

import pytest
from pydantic import Field

from agent.tools import ToolDefinition, ToolRegistry, build_default_registry
from kingdee.endpoints.base import EndpointParams, quote_eq
from kingdee.endpoints.sal_order import SAL_ORDER
from models.base import KingPilotBaseModel
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.guard_limit import LimitGuard
from safety.guard_sql import FilterGuard


class _WideLimitParams(EndpointParams):
    """测试用：limit 不设上下限，用于触达 Guard 4 的边界判定。"""

    limit: int = Field(default=100, description="测试用无约束分页")
    bill_no: str = Field(default="", description="单据编号")


class _BlankFilterParams(EndpointParams):
    """测试用：组装器返回空白过滤串。"""

    bill_no: str = Field(default="", description="单据编号")


class _WriteParams(EndpointParams):
    """测试用写工具参数。"""

    customer_number: str = Field(default="", description="客户编码")


def _bill_filter(params: KingPilotBaseModel) -> str:
    p = cast(_WideLimitParams, params)
    return f"FBillNo = {quote_eq(p.bill_no)}" if p.bill_no else ""


def _blank_filter(params: KingPilotBaseModel) -> str:
    return "   "


def _write_filter(params: KingPilotBaseModel) -> str:
    return ""


_WIDE_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_query_wide",
        description="测试用：无分页上限的查询工具",
        parameters=_WideLimitParams.model_json_schema(),
    ),
    params_model=_WideLimitParams,
    build_filter=_bill_filter,
    endpoint=SAL_ORDER,
)

_BLANK_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_query_blank",
        description="测试用：组装出空白过滤串",
        parameters=_BlankFilterParams.model_json_schema(),
    ),
    params_model=_BlankFilterParams,
    build_filter=_blank_filter,
    endpoint=SAL_ORDER,
)

_WRITE_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_create_order",
        description="测试用写工具",
        parameters=_WriteParams.model_json_schema(),
    ),
    params_model=_WriteParams,
    build_filter=_write_filter,
    endpoint=SAL_ORDER,
    tool_type="write",
)


def _registry() -> ToolRegistry:
    registry = build_default_registry()
    registry.register(_WIDE_TOOL)
    registry.register(_BLANK_TOOL)
    registry.register(_WRITE_TOOL)
    return registry


def _context(tool_name: str, arguments: dict[str, Any]) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments=arguments,
        user_context=UserContext(user_id="u001", role="viewer"),
        trace_id="trace-limit",
    )


# ── 正常与边界 ──


async def test_conditional_query_passes() -> None:
    result = await LimitGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"bill_no": "SO000008", "limit": 50})
    )

    assert result.passed is True
    assert result.detail["limit"] == 50
    assert result.detail["condition_present"] is True


async def test_default_limit_reported() -> None:
    result = await LimitGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"bill_no": "SO000008"})
    )

    assert result.passed is True
    assert result.detail["limit"] == 100


@pytest.mark.parametrize(
    ("limit", "passed"),
    [(1, True), (1001, True), (10000, True), (0, False), (-1, False), (10001, False)],
)
async def test_limit_boundaries(limit: int, passed: bool) -> None:
    result = await LimitGuard(_registry()).check(
        _context("kingdee_query_wide", {"bill_no": "SO1", "limit": limit})
    )

    assert result.passed is passed
    if passed:
        assert result.detail["limit"] == limit
    else:
        assert "超出允许范围" in result.reason


# ── 无条件查询 ──


async def test_unconditional_query_blocked() -> None:
    result = await LimitGuard(_registry()).check(_context("kingdee_query_sales_order", {}))

    assert result.passed is False
    assert "无条件查询" in result.reason
    assert result.detail["filter_empty"] is True


async def test_whitespace_filter_blocked() -> None:
    result = await LimitGuard(_registry()).check(_context("kingdee_query_blank", {}))

    assert result.passed is False
    assert "无条件查询" in result.reason


async def test_write_tool_skips_unconditional_rule() -> None:
    result = await LimitGuard(_registry()).check(
        _context("kingdee_create_order", {"customer_number": "CUS001"})
    )

    assert result.passed is True
    assert result.detail["tool_type"] == "write"
    assert result.detail["condition_present"] is False


# ── fail-closed 分支 ──


async def test_unknown_tool_blocked() -> None:
    result = await LimitGuard(_registry()).check(_context("kingdee_not_exists", {}))

    assert result.passed is False
    assert "未注册" in result.reason


async def test_invalid_params_blocked() -> None:
    result = await LimitGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"limit": [1]})
    )

    assert result.passed is False
    assert "参数校验失败" in result.reason


async def test_build_filter_rejection_blocked() -> None:
    result = await LimitGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"bill_no": "O'Reilly"})
    )

    assert result.passed is False
    assert "组装失败" in result.reason
    assert result.detail["error_type"] == "ValueError"


# ── 与 Guard 2 的职责衔接 ──


async def test_pipeline_guard2_passes_empty_but_guard4_blocks() -> None:
    pipeline = GuardPipeline([FilterGuard(_registry()), LimitGuard(_registry())])

    result = await pipeline.check(_context("kingdee_query_sales_order", {}))

    assert result.blocked is True
    assert result.blocked_by == "guard_limit"
    assert len(result.records) == 2
    assert result.records[0].passed is True  # Guard 2 按设计放行空条件
    assert result.records[0].detail["filter_empty"] is True
