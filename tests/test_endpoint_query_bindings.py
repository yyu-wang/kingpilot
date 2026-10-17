"""M2-T03：端点参数模型与 FilterString 组装单测。

锚定两条不变式：
1. 组装出的条件字段全部落在该端点 ``filterable_fields`` 内（可查 ≠ 可过滤）；
2. 组装格式与金蝶 FilterString 语法逐字符一致（引号/数值格式/AND 连接）。
"""

import pytest
from pydantic import ValidationError

from kingdee.endpoints import (
    CUSTOMER,
    CUSTOMER_TOOL,
    INVENTORY,
    INVENTORY_TOOL,
    PURCHASE_ORDER,
    PURCHASE_ORDER_TOOL,
    SUPPLIER,
    SUPPLIER_TOOL,
    EndpointDescriptor,
)
from kingdee.endpoints.bd_customer import CustomerParams, build_customer_filter
from kingdee.endpoints.pur_order import PurchaseOrderParams, build_purchase_order_filter
from kingdee.endpoints.sal_order import SalesOrderParams, build_sales_order_filter
from kingdee.endpoints.stk_inventory import InventoryParams, build_inventory_filter


@pytest.mark.parametrize(
    ("endpoint", "form_id"),
    [
        pytest.param(CUSTOMER, "BD_Customer", id="customer"),
        pytest.param(SUPPLIER, "BD_Supplier", id="supplier"),
        pytest.param(PURCHASE_ORDER, "PUR_PurchaseOrder", id="purchase_order"),
        pytest.param(INVENTORY, "STK_Inventory", id="inventory"),
    ],
)
def test_endpoint_form_id(endpoint: EndpointDescriptor, form_id: str) -> None:
    """新端点的 FormId 与最小白名单非空（INFERRED 字段集的形状守卫）。"""
    assert endpoint.form_id == form_id
    assert endpoint.selectable_fields
    assert endpoint.default_field_keys


def test_customer_filter_like() -> None:
    """名称模糊查询组装为 LIKE '%值%'。"""
    assert build_customer_filter(CustomerParams(name="测试")) == "FName LIKE '%测试%'"


def test_customer_filter_empty_params() -> None:
    """全默认参数 = 无过滤条件（空 FilterString，查全部）。"""
    assert build_customer_filter(CustomerParams()) == ""


def test_sales_filter_combined_in_field_order() -> None:
    """多条件 AND 连接，顺序 = 参数声明序（可读性锚定）。"""
    result = build_sales_order_filter(
        SalesOrderParams(customer_number="CUS001", document_status="C", min_amount=150000.0)
    )
    assert (
        result
        == "FCustomerID.FNumber = 'CUS001' AND FDocumentStatus = 'C' AND FBillAllAmount >= 150000"
    )


def test_sales_filter_decimal_amount_keeps_point() -> None:
    """非整金额保留小数（fmt_number 只去整数的 .0 尾巴）。"""
    result = build_sales_order_filter(SalesOrderParams(min_amount=99.5))
    assert result == "FBillAllAmount >= 99.5"


def test_sales_filter_bill_no_and_date() -> None:
    """M2-T05 D6 新增字段：单号 / 单日日期精确匹配（评测集 SAL-05/06 的支撑）。"""
    result = build_sales_order_filter(SalesOrderParams(bill_no="SO000008"))
    assert result == "FBillNo = 'SO000008'"
    assert build_sales_order_filter(SalesOrderParams(order_date="2026-03-05")) == (
        "FDate = '2026-03-05'"
    )


def test_sales_filter_combined_five_conditions() -> None:
    """全条件 AND 连接顺序 = 参数声明序（新增字段不破坏既有顺序锚定）。"""
    result = build_sales_order_filter(
        SalesOrderParams(
            customer_number="CUS001",
            document_status="C",
            min_amount=150000.0,
            bill_no="SO000008",
            order_date="2026-03-05",
        )
    )
    assert result == (
        "FCustomerID.FNumber = 'CUS001' AND FDocumentStatus = 'C'"
        " AND FBillAllAmount >= 150000 AND FBillNo = 'SO000008' AND FDate = '2026-03-05'"
    )


def test_sales_rejects_invalid_document_status() -> None:
    """非法枚举值 fail-closed（LLM 传 "X" 直接拒绝，不进组装层）。"""
    with pytest.raises(ValidationError, match="单据状态"):
        SalesOrderParams(document_status="X")


def test_purchase_filter_combined() -> None:
    """采购订单双条件组装（供应商编码 + 状态）。"""
    result = build_purchase_order_filter(
        PurchaseOrderParams(supplier_number="SUP001", document_status="B")
    )
    assert result == "FSupplierID.FNumber = 'SUP001' AND FDocumentStatus = 'B'"


def test_purchase_shares_status_validator() -> None:
    """采购订单与销售订单共用同一状态值域校验。"""
    with pytest.raises(ValidationError, match="单据状态"):
        PurchaseOrderParams(document_status="X")


def test_inventory_filter_material_number() -> None:
    """库存按物料编码精确匹配。"""
    assert build_inventory_filter(InventoryParams(material_number="MAT001")) == (
        "FMaterialId.FNumber = 'MAT001'"
    )


def test_customer_filter_rejects_embedded_quote() -> None:
    """LIKE 值内嵌单引号被组装层拒绝（纵深防御；M4 Guard 2 是主防线）。"""
    with pytest.raises(ValueError, match="单引号"):
        build_customer_filter(CustomerParams(name="o'reilly"))


def test_sales_filter_rejects_embedded_quote() -> None:
    """等值条件同样拒绝单引号。"""
    with pytest.raises(ValueError, match="单引号"):
        build_sales_order_filter(SalesOrderParams(customer_number="a'b"))


def test_tool_bindings_are_consistent() -> None:
    """绑定三件套的 endpoint/params_model 指向同模块常量（装配期防接错线）。"""
    assert CUSTOMER_TOOL.endpoint is CUSTOMER
    assert CUSTOMER_TOOL.params_model is CustomerParams
    assert SUPPLIER_TOOL.endpoint is SUPPLIER
    assert INVENTORY_TOOL.endpoint is INVENTORY
    assert PURCHASE_ORDER_TOOL.endpoint is PURCHASE_ORDER
