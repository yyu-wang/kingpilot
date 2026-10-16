"""M1-T06a：FilterString 受限解析单测（纯函数，零网络）。

锚点：解析结果是「条件元组」，断言字段/比较符/字面量三元组本身，
而不是断言「没抛异常」——后者会让实现把不支持语法静默吞掉也算通过。
"""

import pytest

from sandbox.kingdee_stub.filter_parser import Condition, FilterSyntaxError, parse_filter


def test_parse_empty_filter_returns_empty_tuple() -> None:
    assert parse_filter("") == ()
    assert parse_filter("   ") == ()


def test_parse_single_quoted_equality() -> None:
    assert parse_filter("FDocumentStatus = 'C'") == (Condition("FDocumentStatus", "=", "C"),)


def test_parse_unquoted_numeric_greater_than() -> None:
    assert parse_filter("FBillAllAmount > 100000") == (Condition("FBillAllAmount", ">", "100000"),)


def test_parse_multiple_conditions_joined_by_and() -> None:
    conditions = parse_filter("FDate >= '2026-01-01' AND FQty > 5")
    assert conditions == (
        Condition("FDate", ">=", "2026-01-01"),
        Condition("FQty", ">", "5"),
    )


def test_parse_related_field_with_dot() -> None:
    assert parse_filter("FCustomerID.FNumber <> 'CUS001'") == (
        Condition("FCustomerID.FNumber", "<>", "CUS001"),
    )


def test_parse_like_pattern_keeps_wildcards() -> None:
    assert parse_filter("FBillNo LIKE '%0000%'") == (Condition("FBillNo", "LIKE", "%0000%"),)


def test_parse_lowercase_like_is_accepted_and_normalized() -> None:
    assert parse_filter("FBillNo like 'SO%'") == (Condition("FBillNo", "LIKE", "SO%"),)


def test_parse_and_inside_quotes_is_literal_not_separator() -> None:
    """引号内的 ``AND`` 是字面量，不能被当成条件连接符。"""
    assert parse_filter("FNote = 'A AND B'") == (Condition("FNote", "=", "A AND B"),)


def test_parse_or_inside_quotes_is_literal() -> None:
    """引号内的 ``OR`` 是字面量，不能触发「不支持 OR」的报错。"""
    assert parse_filter("FNote = 'A OR B'") == (Condition("FNote", "=", "A OR B"),)


def test_parse_standalone_or_raises() -> None:
    with pytest.raises(FilterSyntaxError, match="OR"):
        parse_filter("FDocumentStatus = 'C' OR FDocumentStatus = 'B'")


def test_parse_parenthesis_raises() -> None:
    with pytest.raises(FilterSyntaxError, match="括号"):
        parse_filter("(FDocumentStatus = 'C')")


def test_parse_not_raises() -> None:
    with pytest.raises(FilterSyntaxError, match="NOT"):
        parse_filter("FBillNo = 'SO000001' NOT FQty > 1")


def test_parse_unterminated_quote_raises() -> None:
    with pytest.raises(FilterSyntaxError, match="未闭合"):
        parse_filter("FBillNo = 'SO000001")


def test_parse_condition_without_operator_raises() -> None:
    with pytest.raises(FilterSyntaxError, match="无法解析"):
        parse_filter("FBillNo")


def test_parse_field_like_or_is_not_mistaken_for_operator() -> None:
    """字段名 ``FSaleOrgId`` 含 "Or"，不能触发「不支持 OR」的误判（词边界保护）。"""
    assert parse_filter("FSaleOrgId.FNumber = '100'") == (
        Condition("FSaleOrgId.FNumber", "=", "100"),
    )
