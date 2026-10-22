"""``kingdee/filter_string.py`` 单测（T02：FilterExpression → FilterString 渲染器）。

锚定三条不变式：
1. 字面量格式化（``quote_eq`` / ``quote_like`` / ``fmt_number``）与既有
   ``kingdee/endpoints/base.py`` 的行为逐字符一致（这是它们的新家）。
2. ``render_filter_expression`` 输出的语法与既有 ``build_*_filter`` 的拼接
   语义一致（``field <op> <literal>`` 多条件用 `` AND `` 连接）。
3. LIKE 的值类型约束在渲染期就拒绝（``value`` 必须是 ``str``），
   不让非法形状进入 FilterString。
"""

import pytest

from erp import FilterCondition, FilterExpression, FilterOp, eq, ge, gt, le, like, lt, ne
from kingdee.filter_string import (
    FilterSyntaxError,
    ParsedCondition,
    fmt_number,
    parse_filter_string,
    quote_eq,
    quote_like,
    render_filter_expression,
)


class TestQuoteEq:
    """等值条件的带引号字面量（从 endpoints/base.py 迁移，行为不变）。"""

    def test_plain_string_quoted(self) -> None:
        assert quote_eq("CUS001") == "'CUS001'"

    def test_date_string_quoted(self) -> None:
        """date 字段的 ``"YYYY-MM-DD"`` 字符串同样加引号。"""
        assert quote_eq("2026-03-05") == "'2026-03-05'"

    def test_single_quote_rejected(self) -> None:
        """单引号拒绝（金蝶转义规则各版本不一，拒绝 = 把模糊性挡在边界外）。"""
        with pytest.raises(ValueError, match="过滤值不能包含单引号"):
            quote_eq("O'Brien")

    def test_empty_string_quoted(self) -> None:
        assert quote_eq("") == "''"


class TestQuoteLike:
    """模糊匹配字面量（% 通配符包裹）。"""

    def test_wraps_percent_wildcards(self) -> None:
        assert quote_like("测试") == "'%测试%'"

    def test_single_quote_rejected(self) -> None:
        with pytest.raises(ValueError, match="过滤值不能包含单引号"):
            quote_like("O'Brien")


class TestFmtNumber:
    """数值字面量：整数值去 .0 尾巴，非整数保留小数。"""

    def test_integer_strips_trailing_zero(self) -> None:
        assert fmt_number(150000.0) == "150000"

    def test_decimal_keeps_point(self) -> None:
        assert fmt_number(99.5) == "99.5"

    def test_zero(self) -> None:
        assert fmt_number(0.0) == "0"

    def test_negative(self) -> None:
        assert fmt_number(-42.0) == "-42"


class TestRenderFilterExpression:
    """渲染主入口。"""

    def test_empty_expression_renders_empty_string(self) -> None:
        """无条件 = 空 FilterString（查全部，拒绝归 Guard 4）。"""
        assert render_filter_expression(FilterExpression()) == ""

    def test_single_eq_string(self) -> None:
        expr = FilterExpression(conditions=(eq("FCustomerID.FNumber", "CUS001"),))
        assert render_filter_expression(expr) == "FCustomerID.FNumber = 'CUS001'"

    def test_single_eq_date(self) -> None:
        expr = FilterExpression(conditions=(eq("FDate", "2026-03-05"),))
        assert render_filter_expression(expr) == "FDate = '2026-03-05'"

    def test_single_ge_number_integer(self) -> None:
        expr = FilterExpression(conditions=(ge("FBillAllAmount", 150000.0),))
        assert render_filter_expression(expr) == "FBillAllAmount >= 150000"

    def test_single_ge_number_decimal(self) -> None:
        expr = FilterExpression(conditions=(ge("FBillAllAmount", 99.5),))
        assert render_filter_expression(expr) == "FBillAllAmount >= 99.5"

    def test_single_like(self) -> None:
        expr = FilterExpression(conditions=(like("FName", "测试"),))
        assert render_filter_expression(expr) == "FName LIKE '%测试%'"

    def test_multiple_conditions_joined_with_and(self) -> None:
        """多条件用 AND 连接，顺序 = 声明序（与既有 build_*_filter 一致）。"""
        expr = FilterExpression(
            conditions=(
                eq("FCustomerID.FNumber", "CUS001"),
                eq("FDocumentStatus", "C"),
                ge("FBillAllAmount", 150000.0),
            )
        )
        assert render_filter_expression(expr) == (
            "FCustomerID.FNumber = 'CUS001' AND FDocumentStatus = 'C' AND FBillAllAmount >= 150000"
        )

    def test_all_operators_render(self) -> None:
        """七种操作符的渲染符号锚定。"""
        expr = FilterExpression(
            conditions=(
                eq("F1", "v"),
                ne("F2", "v"),
                gt("F3", 1.0),
                ge("F4", 1.0),
                lt("F5", 1.0),
                le("F6", 1.0),
                like("F7", "v"),
            )
        )
        assert render_filter_expression(expr) == (
            "F1 = 'v' AND F2 <> 'v' AND F3 > 1 AND F4 >= 1 AND F5 < 1 AND F6 <= 1 AND F7 LIKE '%v%'"
        )

    def test_like_with_non_string_value_rejected(self) -> None:
        """LIKE 的值必须是 str（float 进入 LIKE 是形状错误，渲染期拦截）。"""
        condition = FilterCondition(field="FName", op=FilterOp.LIKE, value=1.0)
        expr = FilterExpression(conditions=(condition,))
        with pytest.raises(ValueError, match="LIKE 条件的值必须是字符串"):
            render_filter_expression(expr)

    def test_single_quote_in_value_rejected_via_quote_eq(self) -> None:
        """注入尝试在渲染期被 quote_eq 拒绝（防御深度：组装层之外的第二道）。

        构造期 invariant（erp/conditions._check_value）已是第一道防线；本测试
        直接构造 FilterCondition 绕过助手，验证渲染器自身的二次拒绝仍然有效。
        """
        condition = FilterCondition(field="FName", op=FilterOp.EQ, value="x' OR '1'='1")
        expr = FilterExpression(conditions=(condition,))
        with pytest.raises(ValueError, match="过滤值不能包含单引号"):
            render_filter_expression(expr)


class TestParseFilterString:
    """``parse_filter_string``（T04 起从 safety/guard_sql.py 下沉）的直测。"""

    def test_parses_two_conditions(self) -> None:
        assert parse_filter_string("FBillNo = 'SO1' AND FQty >= 2") == (
            ParsedCondition("FBillNo", "=", "SO1"),
            ParsedCondition("FQty", ">=", "2"),
        )

    def test_empty_returns_empty_tuple(self) -> None:
        assert parse_filter_string("   ") == ()

    def test_quoted_keywords_are_literals(self) -> None:
        """引号内的 SQL 关键字/注释/分号是字面量，不命中黑名单（掩码生效）。"""
        conditions = parse_filter_string("FCustomerID.FNumber = 'UPDATE OR DROP; --'")
        assert conditions == (ParsedCondition("FCustomerID.FNumber", "=", "UPDATE OR DROP; --"),)


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


class TestParseFilterStringBlacklist:
    """黑名单自检（注入样例全集拦截）。

    这套用例原本在 ``test_guard_sql.py`` 通过 Guard 2 端到端触发；T04 起
    黑名单设施下沉到渲染器模块，用例改为直接打 ``parse_filter_string``，
    断言形状更窄、定位更准。Guard 2 端到端拦截行为仍由
    ``test_guard_sql.py`` 的代表性样例覆盖。
    """

    @pytest.mark.parametrize(("raw", "expected_reason"), _INJECTION_CASES)
    def test_injection_raises_syntax_error(self, raw: str, expected_reason: str) -> None:
        with pytest.raises(FilterSyntaxError, match=expected_reason):
            parse_filter_string(raw)


class TestRenderSelfCheck:
    """``render_filter_expression`` 的 round-trip 自检（防御渲染器自身 bug）。"""

    def test_rendered_output_is_parseable(self) -> None:
        """渲染产物必须能解析回来（自检通过，不抛 FilterSyntaxError）。"""
        expr = FilterExpression(
            conditions=(
                eq("FCustomerID.FNumber", "CUS001"),
                ge("FBillAllAmount", 150000.0),
                like("FName", "测试"),
            )
        )
        rendered = render_filter_expression(expr)
        conditions = parse_filter_string(rendered)
        assert [c.field for c in conditions] == ["FCustomerID.FNumber", "FBillAllAmount", "FName"]
