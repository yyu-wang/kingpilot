"""M2-T03：端点参数模型与 FilterExpression 组装单测（T05 重写）。

锚定两条不变式：
1. 组装出的条件字段全部落在该端点 ``filterable_fields`` 内（可查 ≠ 可过滤）；
2. 条件顺序 = 参数声明序（可读性锚定），操作符/值与参数语义一一对应。

T05 变更：断言对象从金蝶 FilterString 字符串改为厂商中立 ``FilterExpression``
（渲染为金蝶方言是 ``kingdee/filter_string.py`` 的职责，由渲染器自身单测覆盖）。
"""

import pytest
from pydantic import ValidationError

from erp import FilterExpression, eq, ge, like
from kingdee.endpoints import (
    CUSTOMER,
    CUSTOMER_TOOL,
    INVENTORY,
    INVENTORY_TOOL,
    PURCHASE_INBOUND,
    PURCHASE_INBOUND_TOOL,
    PURCHASE_ORDER,
    PURCHASE_ORDER_TOOL,
    SUPPLIER,
    SUPPLIER_TOOL,
    EndpointDescriptor,
)
from kingdee.endpoints.bd_customer import CustomerParams, build_customer_filter
from kingdee.endpoints.pur_order import PurchaseOrderParams, build_purchase_order_filter
from kingdee.endpoints.sal_order import (
    SAL_ORDER,
    SalesOrderParams,
    build_sales_order_field_keys,
    build_sales_order_filter,
)
from kingdee.endpoints.stk_inbound import (
    PurchaseInboundParams,
    build_purchase_inbound_filter,
)
from kingdee.endpoints.stk_inventory import InventoryParams, build_inventory_filter


@pytest.mark.parametrize(
    ("endpoint", "form_id"),
    [
        pytest.param(CUSTOMER, "BD_Customer", id="customer"),
        pytest.param(SUPPLIER, "BD_Supplier", id="supplier"),
        pytest.param(PURCHASE_ORDER, "PUR_PurchaseOrder", id="purchase_order"),
        pytest.param(PURCHASE_INBOUND, "STK_InStock", id="purchase_inbound"),
        pytest.param(INVENTORY, "STK_Inventory", id="inventory"),
    ],
)
def test_endpoint_form_id(endpoint: EndpointDescriptor, form_id: str) -> None:
    """新端点的 FormId 与最小白名单非空（INFERRED 字段集的形状守卫）。"""
    assert endpoint.form_id == form_id
    assert endpoint.selectable_fields
    assert endpoint.default_field_keys


def test_customer_filter_like() -> None:
    """名称模糊查询组装为 LIKE 条件。"""
    assert build_customer_filter(CustomerParams(name="测试")) == FilterExpression(
        conditions=(like("FName", "测试"),)
    )


def test_customer_filter_empty_params() -> None:
    """全默认参数 = 无过滤条件（空表达式，查全部）。"""
    assert build_customer_filter(CustomerParams()) == FilterExpression()


def test_sales_filter_combined_in_field_order() -> None:
    """多条件 AND 语义，顺序 = 参数声明序（可读性锚定）。"""
    result = build_sales_order_filter(
        SalesOrderParams(customer_number="CUS001", document_status="C", min_amount=150000.0)
    )
    assert result == FilterExpression(
        conditions=(
            eq("FCustomerID.FNumber", "CUS001"),
            eq("FDocumentStatus", "C"),
            ge("FBillAllAmount", 150000.0),
        )
    )


def test_sales_filter_decimal_amount_kept_as_float() -> None:
    """非整金额原样保留为 float（数值格式化是渲染期职责）。"""
    result = build_sales_order_filter(SalesOrderParams(min_amount=99.5))
    assert result == FilterExpression(conditions=(ge("FBillAllAmount", 99.5),))


def test_sales_filter_bill_no_and_date() -> None:
    """M2-T05 D6 新增字段：单号 / 单日日期精确匹配（评测集 SAL-05/06 的支撑）。"""
    result = build_sales_order_filter(SalesOrderParams(bill_no="SO000008"))
    assert result == FilterExpression(conditions=(eq("FBillNo", "SO000008"),))
    assert build_sales_order_filter(SalesOrderParams(order_date="2026-03-05")) == (
        FilterExpression(conditions=(eq("FDate", "2026-03-05"),))
    )


def test_sales_field_keys_expand_entries_only_when_requested() -> None:
    """默认表头字段；include_entries=true 时追加明细字段且全部在白名单内。"""
    assert build_sales_order_field_keys(SalesOrderParams(bill_no="SO000001")) is None

    field_keys = build_sales_order_field_keys(
        SalesOrderParams(bill_no="SO000001", include_entries=True)
    )
    assert field_keys is not None
    assert set(SAL_ORDER.default_field_keys) <= set(field_keys)
    for key in ("FMaterialId.FNumber", "FMaterialId.FName", "FQty", "FTaxPrice"):
        assert key in field_keys
    assert set(field_keys) <= SAL_ORDER.selectable_fields


def test_sales_filter_combined_five_conditions() -> None:
    """全条件 AND 语义顺序 = 参数声明序（新增字段不破坏既有顺序锚定）。"""
    result = build_sales_order_filter(
        SalesOrderParams(
            customer_number="CUS001",
            document_status="C",
            min_amount=150000.0,
            bill_no="SO000008",
            order_date="2026-03-05",
        )
    )
    assert result == FilterExpression(
        conditions=(
            eq("FCustomerID.FNumber", "CUS001"),
            eq("FDocumentStatus", "C"),
            ge("FBillAllAmount", 150000.0),
            eq("FBillNo", "SO000008"),
            eq("FDate", "2026-03-05"),
        )
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
    assert result == FilterExpression(
        conditions=(
            eq("FSupplierID.FNumber", "SUP001"),
            eq("FDocumentStatus", "B"),
        )
    )


def test_purchase_filter_supports_amount_bill_no_and_date() -> None:
    """采购订单与销售订单同口径：金额下限、单号、日期均可过滤。"""
    result = build_purchase_order_filter(
        PurchaseOrderParams(
            min_amount=50_000,
            bill_no="PO000002",
            order_date="2026-01-08",
        )
    )
    assert result == FilterExpression(
        conditions=(
            ge("FAmount", 50_000.0),
            eq("FBillNo", "PO000002"),
            eq("FDate", "2026-01-08"),
        )
    )


def test_purchase_filter_supports_material_and_quantity() -> None:
    """采购订单可按物料编码与数量下限过滤（新增单物料行字段）。"""
    result = build_purchase_order_filter(
        PurchaseOrderParams(material_number="MAT001", min_quantity=10)
    )
    assert result == FilterExpression(
        conditions=(
            eq("FMaterialId.FNumber", "MAT001"),
            ge("FQty", 10.0),
        )
    )


def test_purchase_shares_status_validator() -> None:
    """采购订单与销售订单共用同一状态值域校验。"""
    with pytest.raises(ValidationError, match="单据状态"):
        PurchaseOrderParams(document_status="X")


def test_purchase_inbound_filter_full() -> None:
    """采购入库单：源单号/供应商/物料/仓库/状态/日期/数量全条件组装。"""
    result = build_purchase_inbound_filter(
        PurchaseInboundParams(
            bill_no="CGRK000001",
            supplier_number="SUP001",
            material_number="MAT001",
            warehouse_name="成品仓",
            document_status="C",
            order_date="2026-01-06",
            source_bill_no="PO000001",
            min_quantity=10,
        )
    )
    assert result == FilterExpression(
        conditions=(
            eq("FBillNo", "CGRK000001"),
            eq("FSupplierID.FNumber", "SUP001"),
            eq("FMaterialId.FNumber", "MAT001"),
            eq("FStockId.FName", "成品仓"),
            eq("FDocumentStatus", "C"),
            eq("FDate", "2026-01-06"),
            eq("FSrcBillNo", "PO000001"),
            ge("FRealQty", 10.0),
        )
    )


def test_purchase_inbound_shares_status_validator() -> None:
    with pytest.raises(ValidationError, match="单据状态"):
        PurchaseInboundParams(document_status="X")


def test_inventory_filter_material_number() -> None:
    """库存按物料编码精确匹配。"""
    assert build_inventory_filter(InventoryParams(material_number="MAT001")) == (
        FilterExpression(conditions=(eq("FMaterialId.FNumber", "MAT001"),))
    )


def test_customer_filter_rejects_embedded_quote() -> None:
    """LIKE 值内嵌单引号被构造期拒绝（公共注入面 invariant，见 erp/conditions.py）。"""
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
    assert PURCHASE_INBOUND_TOOL.endpoint is PURCHASE_INBOUND
    assert PURCHASE_INBOUND_TOOL.params_model is PurchaseInboundParams
