"""M1-T06a：种子数据集完整性单测。

锚点：数据集必须覆盖「跨页」「全状态」「明细展开」三类边界，
否则查询端点测试就无法真正触达分页与展开分支（假绿风险）。
"""

from kingdee.endpoints.pur_order import PURCHASE_ORDER
from kingdee.endpoints.sal_order import SAL_ORDER
from kingdee.endpoints.stk_inbound import PURCHASE_INBOUND
from sandbox.kingdee_stub.dataset import (
    ALL_STATUS_VALUES,
    CUSTOMER_ROWS,
    ENTRY_FIELD_NAMES,
    INVENTORY_ROWS,
    MATERIAL_ROWS,
    ORDERS,
    PURCHASE_INBOUND_ROWS,
    PURCHASE_INBOUND_STATUS_COVERAGE,
    PURCHASE_ORDERS,
    PURCHASE_STATUS_COVERAGE,
    STATUS_COVERAGE,
    SUPPLIER_ROWS,
    SalesOrder,
    entry_value,
    has_entry_field,
    header_value,
    purchase_inbound_value,
    purchase_order_value,
    supplier_value,
)


def test_dataset_covers_every_document_status() -> None:
    assert STATUS_COVERAGE == ALL_STATUS_VALUES


def test_dataset_exceeds_default_limit_for_paging() -> None:
    """行数必须超过默认 ``Limit=100``，否则跨页分支永远不被触达。"""
    assert len(ORDERS) > 100


def test_dataset_order_numbers_are_unique_and_sorted() -> None:
    bill_numbers = [order.bill_no for order in ORDERS]
    assert len(set(bill_numbers)) == len(bill_numbers)
    assert bill_numbers == sorted(bill_numbers)


def test_dataset_has_an_order_with_multiple_entries() -> None:
    assert any(len(order.entries) > 1 for order in ORDERS)


def test_entry_field_detection() -> None:
    assert has_entry_field(["FBillNo", "FQty"]) is True
    assert has_entry_field(["FBillNo", "FDate"]) is False
    assert ENTRY_FIELD_NAMES.issubset(SAL_ORDER.selectable_fields)


def test_accessors_return_declared_values() -> None:
    first: SalesOrder = ORDERS[0]
    assert header_value(first, "FBillNo") == first.bill_no
    assert entry_value(first, 0, "FQty") == first.entries[0].qty
    assert entry_value(first, 0, "FEntryID") == "1"
    # 明细物料名称从基础资料反查（对齐真实金蝶 FMaterialId.FName）
    assert entry_value(first, 0, "FMaterialId.FName") == "测试物料001"
    # 表头字段访问器对明细字段不适用，应返回 None（上层已先做白名单校验）
    assert header_value(first, "FQty") is None


# ── M9 测试数据补全：主数据与采购订单 ──


def test_master_data_numbers_and_ids_are_unique() -> None:
    for rows, label in (
        (CUSTOMER_ROWS, "customer"),
        (SUPPLIER_ROWS, "supplier"),
        (MATERIAL_ROWS, "material"),
    ):
        numbers = [row.number for row in rows]
        fids = [row.fid for row in rows]
        assert len(set(numbers)) == len(numbers), f"{label} 编码重复"
        assert len(set(fids)) == len(fids), f"{label} FID 重复"


def test_master_data_has_extended_and_zero_stock_materials() -> None:
    material_numbers = {row.number for row in MATERIAL_ROWS}
    assert {"MAT006", "MAT007", "MAT008"}.issubset(material_numbers)

    inventory = {row.material_number: row for row in INVENTORY_ROWS}
    assert inventory["MAT007"].base_qty == "0.0000000000"  # 物料存在但零库存
    assert "MAT001" in inventory  # 多仓聚合锚点仍保留


def test_purchase_orders_cover_all_statuses_and_are_unique() -> None:
    bill_numbers = [order.bill_no for order in PURCHASE_ORDERS]
    assert len(bill_numbers) >= 30
    assert len(set(bill_numbers)) == len(bill_numbers)
    assert bill_numbers == sorted(bill_numbers)
    assert PURCHASE_STATUS_COVERAGE == ALL_STATUS_VALUES


def test_purchase_orders_reference_existing_suppliers() -> None:
    supplier_numbers = {row.number for row in SUPPLIER_ROWS}
    assert {order.supplier_number for order in PURCHASE_ORDERS}.issubset(supplier_numbers)


def test_purchase_order_accessors_and_anchor() -> None:
    assert PURCHASE_ORDER.selectable_fields == {
        "FBillNo",
        "FDate",
        "FDocumentStatus",
        "FSupplierID.FNumber",
        "FSupplierID.FName",
        "FAmount",
        "FMaterialId.FNumber",
        "FMaterialId.FName",
        "FQty",
        "FTaxPrice",
    }
    anchor = next(order for order in PURCHASE_ORDERS if order.bill_no == "PO000002")
    assert purchase_order_value(anchor, "FSupplierID.FNumber") == "SUP002"
    assert purchase_order_value(anchor, "FAmount") == "160000.0000000000"
    assert purchase_order_value(anchor, "FDate") == "2026-01-08"
    assert purchase_order_value(anchor, "FMaterialId.FNumber") == "MAT002"
    assert purchase_order_value(anchor, "FQty") == "160.0000000000"
    assert purchase_order_value(anchor, "FTaxPrice") == "1000.0000000000"
    assert purchase_order_value(anchor, "FNotDeclared") is None

    supplier = next(row for row in SUPPLIER_ROWS if row.number == "SUP004")
    assert supplier_value(supplier, "FName") == "深圳YY贸易有限公司"
    assert supplier_value(supplier, "FID") == "4004"


# ── 采购入库流程：入库单必须能回链采购订单 ──


def test_purchase_inbounds_cover_statuses_and_unique_bill_numbers() -> None:
    bill_numbers = [row.bill_no for row in PURCHASE_INBOUND_ROWS]
    assert len(bill_numbers) >= 10
    assert len(set(bill_numbers)) == len(bill_numbers)
    assert bill_numbers == sorted(bill_numbers)
    assert PURCHASE_INBOUND_STATUS_COVERAGE == ALL_STATUS_VALUES


def test_purchase_inbounds_reference_existing_orders_and_suppliers() -> None:
    order_bills = {order.bill_no for order in PURCHASE_ORDERS}
    order_suppliers = {order.bill_no: order.supplier_number for order in PURCHASE_ORDERS}
    supplier_numbers = {row.number for row in SUPPLIER_ROWS}

    for inbound in PURCHASE_INBOUND_ROWS:
        assert inbound.source_bill_no in order_bills
        assert inbound.supplier_number == order_suppliers[inbound.source_bill_no]
        assert inbound.supplier_number in supplier_numbers


def test_purchase_inbound_accessors_and_anchor() -> None:
    assert PURCHASE_INBOUND.selectable_fields == {
        "FBillNo",
        "FDate",
        "FDocumentStatus",
        "FSupplierID.FNumber",
        "FSupplierID.FName",
        "FStockId.FName",
        "FMaterialId.FNumber",
        "FMaterialId.FName",
        "FRealQty",
        "FSrcBillNo",
        "FAmount",
    }
    anchor = next(row for row in PURCHASE_INBOUND_ROWS if row.bill_no == "CGRK000001")
    assert purchase_inbound_value(anchor, "FSrcBillNo") == "PO000001"
    assert purchase_inbound_value(anchor, "FSupplierID.FNumber") == "SUP001"
    assert purchase_inbound_value(anchor, "FMaterialId.FNumber") == "MAT001"
    assert purchase_inbound_value(anchor, "FStockId.FName") == "成品仓"
    assert purchase_inbound_value(anchor, "FRealQty") == "50.0000000000"
    assert purchase_inbound_value(anchor, "FNotDeclared") is None
