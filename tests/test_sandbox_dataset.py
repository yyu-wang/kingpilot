"""M1-T06a：种子数据集完整性单测。

锚点：数据集必须覆盖「跨页」「全状态」「明细展开」三类边界，
否则查询端点测试就无法真正触达分页与展开分支（假绿风险）。
"""

from kingdee.endpoints.sal_order import SAL_ORDER
from sandbox.kingdee_stub.dataset import (
    ALL_STATUS_VALUES,
    ENTRY_FIELD_NAMES,
    ORDERS,
    STATUS_COVERAGE,
    SalesOrder,
    entry_value,
    has_entry_field,
    header_value,
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
    # 表头字段访问器对明细字段不适用，应返回 None（上层已先做白名单校验）
    assert header_value(first, "FQty") is None
