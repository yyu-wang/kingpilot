"""业务一致性预检（采购入库）单测：源单状态 / 供应商一致 / 仓库存在。"""

from typing import Any, cast

from erp.protocol import ErpClient
from models.kingdee import BillQueryResult, JsonScalar
from models.user import UserContext
from safety.guard import GuardContext
from safety.guard_business import InboundPreflightGuard

_TOOL = "erp_create_purchase_inbound"
_USER = UserContext(user_id="wanda", role="warehouse")


class _FakeClient:
    """按 FormId 返回预置行的查询替身。"""

    def __init__(
        self,
        orders: list[dict[str, JsonScalar]] | None = None,
        stocks: list[dict[str, JsonScalar]] | None = None,
    ) -> None:
        self._orders = orders or []
        self._stocks = stocks or []

    async def query(self, params: Any) -> BillQueryResult:
        rows = self._orders if params.form_id == "PUR_PurchaseOrder" else self._stocks
        return BillQueryResult(form_id=params.form_id, field_keys=[], rows=rows)


def _guard(client: _FakeClient) -> InboundPreflightGuard:
    return InboundPreflightGuard(cast(ErpClient, client))


def _context(**arguments: Any) -> GuardContext:
    return GuardContext(tool_name=_TOOL, arguments=arguments, user_context=_USER)


_AUDITED_ORDER: dict[str, JsonScalar] = {
    "FBillNo": "PO000001",
    "FDocumentStatus": "C",
    "FSupplierID.FNumber": "SUP001",
    "FSupplierID.FName": "测试供应商001",
}
_STOCK: dict[str, JsonScalar] = {"FNumber": "STK001", "FName": "成品仓"}


async def test_non_inbound_tool_is_skipped() -> None:
    guard = _guard(_FakeClient())
    context = GuardContext(tool_name="erp_create_sales_order", arguments={}, user_context=_USER)

    result = await guard.check(context)

    assert result.passed is True
    assert result.detail["skipped"] is True


async def test_missing_source_bill_is_left_to_param_guard() -> None:
    guard = _guard(_FakeClient())

    result = await guard.check(_context(supplier_number="SUP001"))

    assert result.passed is True
    assert result.detail["skipped"] is True


async def test_missing_source_order_is_blocked() -> None:
    guard = _guard(_FakeClient(orders=[]))

    result = await guard.check(_context(source_bill_no="PO999999"))

    assert result.passed is False
    assert "源采购订单不存在" in result.reason
    assert result.detail["branch"] == "source_missing"


async def test_unaudited_source_order_is_blocked() -> None:
    order = {**_AUDITED_ORDER, "FDocumentStatus": "Z"}
    guard = _guard(_FakeClient(orders=[order]))

    result = await guard.check(_context(source_bill_no="PO000004"))

    assert result.passed is False
    assert "源采购订单未审核" in result.reason
    assert result.detail["document_status"] == "Z"


async def test_supplier_mismatch_is_blocked() -> None:
    guard = _guard(_FakeClient(orders=[_AUDITED_ORDER]))

    result = await guard.check(_context(source_bill_no="PO000001", supplier_number="SUP002"))

    assert result.passed is False
    assert "供应商与源采购订单不一致" in result.reason
    assert result.detail["branch"] == "supplier_mismatch"


async def test_supplier_name_match_is_allowed() -> None:
    guard = _guard(_FakeClient(orders=[_AUDITED_ORDER], stocks=[_STOCK]))

    result = await guard.check(
        _context(
            source_bill_no="PO000001",
            supplier_number="测试供应商001",
            warehouse_name="成品仓",
        )
    )

    assert result.passed is True
    assert result.detail["branch"] == "all_valid"


async def test_missing_warehouse_is_blocked() -> None:
    guard = _guard(_FakeClient(orders=[_AUDITED_ORDER], stocks=[]))

    result = await guard.check(_context(source_bill_no="PO000001", warehouse_name="不存在的仓库"))

    assert result.passed is False
    assert "仓库不存在" in result.reason
    assert result.detail["branch"] == "warehouse_missing"
