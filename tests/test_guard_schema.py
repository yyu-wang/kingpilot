"""M4-T02：Guard 1（Schema 校验）单测。

覆盖验收点：未注册 Tool / 缺必填参数 / 类型错误全部拦截；
外加取值越界、未知参数、枚举非法、空参数放行与管道集成。
"""

from typing import Any

import pytest
from pydantic import Field

from agent.tools import ToolDefinition, ToolRegistry, build_default_registry
from kingdee.endpoints.base import EndpointParams
from kingdee.endpoints.sal_order import SAL_ORDER
from models.base import KingPilotBaseModel
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.guard_schema import SchemaGuard


class _BillRequiredParams(EndpointParams):
    """测试用参数模型：内置查询工具参数全部可选，无法覆盖「缺参」分支。"""

    bill_no: str = Field(description="单据编号（必填）")


def _empty_filter(params: KingPilotBaseModel) -> str:
    return ""


_REQUIRED_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_query_required_bill",
        description="测试用：按单据编号查询",
        parameters=_BillRequiredParams.model_json_schema(),
    ),
    params_model=_BillRequiredParams,
    build_filter=_empty_filter,
    endpoint=SAL_ORDER,
)


def _registry() -> ToolRegistry:
    registry = build_default_registry()
    registry.register(_REQUIRED_TOOL)
    return registry


def _context(tool_name: str, arguments: dict[str, Any]) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments=arguments,
        user_context=UserContext(user_id="u001", role="viewer"),
        trace_id="trace-schema",
    )


# ── 放行 ──


async def test_valid_arguments_pass() -> None:
    result = await SchemaGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"bill_no": "SO000008", "limit": 50})
    )

    assert result.passed is True
    assert result.reason == ""
    assert result.detail == {"tool": "kingdee_query_sales_order", "param_count": 2}


async def test_empty_arguments_pass_when_all_optional() -> None:
    """空条件不是 Guard 1 的职责（T05 才拒绝无条件查询）。"""
    result = await SchemaGuard(_registry()).check(_context("kingdee_query_sales_order", {}))

    assert result.passed is True
    assert result.detail["param_count"] == 0


# ── 拦截：工具名白名单 ──


async def test_unknown_tool_blocked() -> None:
    result = await SchemaGuard(_registry()).check(_context("kingdee_drop_table", {}))

    assert result.passed is False
    assert "未注册" in result.reason
    known_tools = result.detail["known_tools"]
    assert "kingdee_query_sales_order" in known_tools
    assert "kingdee_drop_table" not in known_tools


# ── 拦截：必填 / 类型 / 取值域 ──


async def test_missing_required_param_blocked() -> None:
    result = await SchemaGuard(_registry()).check(
        _context("kingdee_query_required_bill", {"limit": 10})
    )

    assert result.passed is False
    assert "参数校验失败" in result.reason
    assert "bill_no" in result.reason
    errors = result.detail["errors"]
    assert errors[0]["field"] == "bill_no"
    assert errors[0]["type"] == "missing"


async def test_wrong_type_blocked() -> None:
    result = await SchemaGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"limit": "abc"})
    )

    assert result.passed is False
    assert "limit" in result.reason
    assert result.detail["errors"][0]["type"] == "int_parsing"


@pytest.mark.parametrize(
    ("limit", "error_type"), [(0, "greater_than_equal"), (10001, "less_than_equal")]
)
async def test_out_of_range_blocked(limit: int, error_type: str) -> None:
    result = await SchemaGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"limit": limit})
    )

    assert result.passed is False
    assert result.detail["errors"][0]["type"] == error_type


async def test_unknown_argument_blocked() -> None:
    """LLM 自创参数（如拼 SQL）在 Schema 层直接拒绝（extra=forbid）。"""
    result = await SchemaGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"sql": "DROP TABLE T_SAL_ORDER"})
    )

    assert result.passed is False
    assert result.detail["errors"][0]["field"] == "sql"
    assert result.detail["errors"][0]["type"] == "extra_forbidden"


async def test_invalid_enum_value_blocked() -> None:
    result = await SchemaGuard(_registry()).check(
        _context("kingdee_query_sales_order", {"document_status": "X"})
    )

    assert result.passed is False
    assert "单据状态" in result.reason


# ── 与 T01 管道集成 ──


async def test_guard_works_inside_pipeline() -> None:
    pipeline = GuardPipeline([SchemaGuard(_registry())])

    result = await pipeline.check(_context("kingdee_drop_table", {}))

    assert result.blocked is True
    assert result.blocked_by == "guard_schema"
    assert result.records[0].detail["tool"] == "kingdee_drop_table"
