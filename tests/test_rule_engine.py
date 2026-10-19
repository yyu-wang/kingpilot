"""M6-T01：只读降级规则引擎单测。

覆盖销售/采购订单、客户/供应商、库存、状态组合、写意图拒绝、
可用工具过滤与无条件查询拒绝。
"""

import pytest

from agent.rule_engine import (
    CUSTOMER_TOOL,
    INVENTORY_TOOL,
    PURCHASE_ORDER_TOOL,
    SALES_ORDER_TOOL,
    SUPPLIER_TOOL,
    FallbackPlan,
    RuleEngine,
)


def _match(text: str, available: set[str] | None = None) -> FallbackPlan | None:
    return RuleEngine().match(text, available_tools=available)


def test_knowledge_intent_is_not_downgraded_to_erp() -> None:
    """M9-T09：流程/步骤类知识问题不降级为 ERP 查询。"""
    assert _match("销售订单审核流程是什么？") is None
    assert _match("库存盘点的操作步骤") is None


def test_explicit_business_code_still_matches() -> None:
    """有显式单据编码时，即使带「说明」也仍按 ERP 查询。"""
    result = _match("查一下 SO000008 的订单说明")

    assert result is not None
    assert result.plan.tool_calls[0].tool_name == SALES_ORDER_TOOL


def test_sales_order_by_bill_no() -> None:
    result = _match("查销售订单 SO000008")

    assert result is not None
    assert result.intent == "sales_order"
    call = result.plan.tool_calls[0]
    assert call.tool_name == SALES_ORDER_TOOL
    assert call.arguments == {"bill_no": "SO000008"}
    assert call.rationale == "fallback:sales_order"


def test_sales_order_by_customer_code() -> None:
    result = _match("查一下 CUS001 的订单")

    assert result is not None
    assert result.plan.tool_calls[0].arguments == {"customer_number": "CUS001"}


def test_sales_order_combines_customer_and_status() -> None:
    result = _match("查 CUS001 已审核的订单")

    assert result is not None
    assert result.plan.tool_calls[0].arguments == {
        "customer_number": "CUS001",
        "document_status": "C",
    }


def test_sales_order_status_only() -> None:
    result = _match("查已审核的销售订单")

    assert result is not None
    assert result.plan.tool_calls[0].arguments == {"document_status": "C"}


@pytest.mark.parametrize(
    ("text", "status"),
    [("查未审核订单", "A"), ("查待审核订单", "A"), ("查审核中的订单", "B")],
)
def test_sales_order_status_keywords(text: str, status: str) -> None:
    result = _match(text)

    assert result is not None
    assert result.plan.tool_calls[0].arguments == {"document_status": status}


def test_purchase_order_by_supplier() -> None:
    result = _match("查采购订单 SUP001")

    assert result is not None
    assert result.intent == "purchase_order"
    assert result.plan.tool_calls[0].tool_name == PURCHASE_ORDER_TOOL
    assert result.plan.tool_calls[0].arguments == {"supplier_number": "SUP001"}


def test_purchase_order_by_status() -> None:
    result = _match("查已审核的采购订单")

    assert result is not None
    assert result.plan.tool_calls[0].arguments == {"document_status": "C"}


def test_customer_name_query() -> None:
    result = _match("查客户 测试公司")

    assert result is not None
    assert result.intent == "customer"
    assert result.plan.tool_calls[0].tool_name == CUSTOMER_TOOL
    assert result.plan.tool_calls[0].arguments == {"name": "测试公司"}


def test_supplier_name_query() -> None:
    result = _match("查供应商 深圳供应")

    assert result is not None
    assert result.intent == "supplier"
    assert result.plan.tool_calls[0].tool_name == SUPPLIER_TOOL
    assert result.plan.tool_calls[0].arguments == {"name": "深圳供应"}


def test_inventory_by_material() -> None:
    result = _match("查物料 MAT001 的库存")

    assert result is not None
    assert result.intent == "inventory"
    assert result.plan.tool_calls[0].tool_name == INVENTORY_TOOL
    assert result.plan.tool_calls[0].arguments == {"material_number": "MAT001"}


def test_customer_code_with_order_prefers_sales() -> None:
    result = _match("查客户 CUS001 的订单")

    assert result is not None
    assert result.plan.tool_calls[0].tool_name == SALES_ORDER_TOOL
    assert result.plan.tool_calls[0].arguments == {"customer_number": "CUS001"}


def test_customer_code_only_is_not_customer_query() -> None:
    """客户工具只支持名称模糊查询，纯编码不猜。"""
    assert _match("查客户 CUS001") is None


def test_case_insensitive_codes() -> None:
    result = _match("查订单 so000008")

    assert result is not None
    assert result.plan.tool_calls[0].arguments == {"bill_no": "SO000008"}


# ── 不匹配/写意图 ──


@pytest.mark.parametrize(
    "text",
    [
        "你好",
        "",
        "查订单",
        "查采购订单",
        "查客户",
    ],
)
def test_no_match_without_conditions_or_intent(text: str) -> None:
    assert _match(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "帮我删除 SO000008",
        "创建一张销售订单",
        "修改 SO000008 的金额",
        "把这单提交审核",
        "审核 SO000008",
        "反审核 SO000008",
    ],
)
def test_write_intents_are_rejected(text: str) -> None:
    assert _match(text) is None


def test_status_word_is_not_treated_as_write() -> None:
    """“已审核”是查询状态，不是写操作。"""
    assert _match("查已审核订单") is not None


# ── 可用工具过滤 ──


def test_available_tools_filter() -> None:
    assert _match("查 CUS001 的订单", available={SALES_ORDER_TOOL}) is not None
    assert _match("查 CUS001 的订单", available={CUSTOMER_TOOL}) is None
