"""M4-T03：Guard 2（FilterString 校验）单测。

覆盖验收点：SQL 注入 / 全表 / 注释 / 时间盲注 / 恒真 / UNION 样例 100% 拦截；
外加字段白名单、可查不可过滤、字面量误杀防护、空条件职责边界与管道集成。
"""

from typing import Any, cast

import pytest
from pydantic import Field

from agent.tools import ToolDefinition, ToolRegistry, build_default_registry
from kingdee.endpoints.base import EndpointParams
from kingdee.endpoints.sal_order import SAL_ORDER
from models.base import KingPilotBaseModel
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.guard_sql import FilterCondition, FilterGuard, parse_filter_string


class _RawParams(EndpointParams):
    """测试用参数模型：把 raw 原样作为 FilterString（模拟被污染的组装层）。"""

    raw: str = Field(default="", description="测试用原始过滤串")


def _raw_filter(params: KingPilotBaseModel) -> str:
    return cast(_RawParams, params).raw


_RAW_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_query_raw",
        description="测试用：直接暴露原始 FilterString",
        parameters=_RawParams.model_json_schema(),
    ),
    params_model=_RawParams,
    build_filter=_raw_filter,
    endpoint=SAL_ORDER,
)


def _registry_with_raw() -> ToolRegistry:
    registry = build_default_registry()
    registry.register(_RAW_TOOL)
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
        _context("kingdee_query_sales_order", {"bill_no": "SO000008", "min_amount": 1000.5})
    )

    assert result.passed is True
    assert result.detail["condition_count"] == 2
    assert result.detail["fields"] == ["FBillAllAmount", "FBillNo"]


async def test_like_condition_passes() -> None:
    result = await FilterGuard(build_default_registry()).check(
        _context("kingdee_query_customer", {"name": "测试"})
    )

    assert result.passed is True
    assert result.detail["fields"] == ["FName"]


async def test_quoted_keywords_are_not_false_positive() -> None:
    """引号内的 SQL 关键字/注释/分号是字面量，不能误杀（掩码后不参与匹配）。"""
    result = await FilterGuard(_registry_with_raw()).check(
        _context("kingdee_query_raw", {"raw": "FCustomerID.FNumber = 'UPDATE OR DROP; --'"})
    )

    assert result.passed is True


async def test_empty_filter_passes_by_design() -> None:
    """空条件拒绝归 T05：本 Guard 只校验「有条件的查询是否安全」。"""
    result = await FilterGuard(build_default_registry()).check(
        _context("kingdee_query_sales_order", {})
    )

    assert result.passed is True
    assert result.detail["filter_empty"] is True


# ── 注入样例：全部拦截 ──

_INJECTION_CASES = [
    ("FBillNo = 'SO1' UNION SELECT * FROM T_USER", "UNION"),
    ("FBillNo = 'SO1' union select * from T_USER", "UNION"),
    ("FBillNo = 'SO1'; DROP TABLE T_SAL_ORDER", "危险 SQL"),
    ("FBillNo = 'SO1' -- comment", "注释"),
    ("FBillNo = 'SO1' /* comment */", "注释"),
    ("FBillNo = 'SO1' OR 1=1", "恒真"),
    ("FBillNo = 'SO1' OR '1'='1'", "OR / NOT"),
    ("1=1", "恒真"),
    ("FBillNo = 'SO1' AND 1 = 1", "恒真"),
    ("FBillNo = 'SO1' AND SLEEP(5)", "时间盲注"),
    ("FBillNo = 'SO1' AND BENCHMARK(1000000,MD5(1))", "时间盲注"),
    ("FBillNo = 'SO1' AND (FQty = 1)", "括号"),
    ("FBillNo = 'SO1' AND NOT FDocumentStatus = 'A'", "OR / NOT"),
    ("FBillNo = 'unclosed", "未闭合"),
    ("FBillNo = FBillNo", "字面量"),
    ("FBillNo = 'SO1' AND AND", "无法解析"),
    ("FBillNo = 'a''b'", "单引号"),
    ("FBillNo = 'a\\' OR 1=1 --'", "未闭合"),
]


@pytest.mark.parametrize(("raw", "expected_reason"), _INJECTION_CASES)
async def test_injection_blocked(raw: str, expected_reason: str) -> None:
    result = await FilterGuard(_registry_with_raw()).check(
        _context("kingdee_query_raw", {"raw": raw})
    )

    assert result.passed is False
    assert expected_reason in result.reason
    assert result.detail["filter_string"] == raw


# ── 字段白名单 ──


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        ("FSecret = 'x'", "FSecret"),
        ("FID = 123", "FID"),  # 可查不可过滤
        ("FCustomerID.FName = '客户A'", "FCustomerID.FName"),  # 显示字段不可过滤
    ],
)
async def test_field_whitelist_blocks(raw: str, field: str) -> None:
    result = await FilterGuard(_registry_with_raw()).check(
        _context("kingdee_query_raw", {"raw": raw})
    )

    assert result.passed is False
    assert "白名单" in result.reason
    assert result.detail["field"] == field
    assert "FBillNo" in result.detail["allowed_fields"]


# ── 其它 fail-closed 分支 ──


async def test_unknown_tool_blocked() -> None:
    result = await FilterGuard(build_default_registry()).check(_context("kingdee_drop_table", {}))

    assert result.passed is False
    assert "未注册" in result.reason


async def test_invalid_params_blocked() -> None:
    result = await FilterGuard(build_default_registry()).check(
        _context("kingdee_query_sales_order", {"bill_no": ["不是字符串"]})
    )

    assert result.passed is False
    assert "参数校验失败" in result.reason
    assert result.detail["error_count"] >= 1


async def test_build_filter_rejection_blocked() -> None:
    """组装层拒绝单引号注入值 → Guard 2 归一为拦截。"""
    result = await FilterGuard(build_default_registry()).check(
        _context("kingdee_query_sales_order", {"bill_no": "O'Reilly"})
    )

    assert result.passed is False
    assert "组装失败" in result.reason
    assert result.detail["error_type"] == "ValueError"


# ── 解析器直测与管道集成 ──


def test_parse_filter_string_returns_conditions() -> None:
    assert parse_filter_string("FBillNo = 'SO1' AND FQty >= 2") == (
        FilterCondition("FBillNo", "=", "SO1"),
        FilterCondition("FQty", ">=", "2"),
    )
    assert parse_filter_string("   ") == ()


async def test_guard_works_inside_pipeline() -> None:
    pipeline = GuardPipeline([FilterGuard(_registry_with_raw())])

    result = await pipeline.check(
        _context("kingdee_query_raw", {"raw": "FBillNo = 'x' UNION SELECT 1"})
    )

    assert result.blocked is True
    assert result.blocked_by == "guard_filter"
    assert "filter_string" in result.records[0].detail
