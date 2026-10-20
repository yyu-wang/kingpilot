"""沙箱可变销售订单库（M9-T13）。

职责：
- 以 ``dataset.ORDERS`` 为种子，提供内存增删改查
- 固定状态机：Z/A → B → C → A；删除仅允许 Z/A
- 单号 ``SO9xxxxx`` 递增，金额/数量保留 10 位小数字符串
- 线程锁保护并发写；``reset()`` 供测试 fixture 复位
"""

import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation

from sandbox.kingdee_stub.dataset import (
    CUSTOMER_ROWS,
    MATERIAL_ROWS,
    ORDERS,
    Entry,
    SalesOrder,
)

# 编码 ↔ 内码：写端点可能收到 Guard 3 换码后的 FID，也可能收到原始编码
_CUSTOMER_BY_NUMBER: dict[str, str] = {row.number: row.name for row in CUSTOMER_ROWS}
_CUSTOMER_BY_FID: dict[str, str] = {row.fid: row.number for row in CUSTOMER_ROWS}
_MATERIAL_BY_NUMBER: frozenset[str] = frozenset(row.number for row in MATERIAL_ROWS)
_MATERIAL_BY_FID: dict[str, str] = {row.fid: row.number for row in MATERIAL_ROWS}

_BILL_SEQ_START = 900_001
_FID_START = 900_001
_AMOUNT_PRECISION = 10
_DEFAULT_SALE_ORG = "100"
_DEFAULT_CURRENCY = "PRE001"
_DEFAULT_BILL_TYPE = "Standard"


class SalesOrderWriteError(Exception):
    """沙箱写操作业务错误（由 HTTP 层转成金蝶错误信封）。"""

    def __init__(self, message: str, *, code: int = 21) -> None:
        super().__init__(message)
        self.code = code


class SalesOrderStore:
    """进程内可变销售订单库。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders: dict[str, SalesOrder] = {}
        self._next_seq = _BILL_SEQ_START
        self._next_fid = _FID_START
        self.reset()

    def reset(self) -> None:
        """恢复为固定种子数据（测试/重启复位）。"""
        with self._lock:
            self._orders = {order.bill_no: order for order in ORDERS}
            self._next_seq = _BILL_SEQ_START
            self._next_fid = _FID_START

    def orders(self) -> tuple[SalesOrder, ...]:
        """按单号升序返回当前订单快照。"""
        with self._lock:
            return tuple(sorted(self._orders.values(), key=lambda order: order.bill_no))

    def get(self, bill_no: str) -> SalesOrder | None:
        with self._lock:
            return self._orders.get(bill_no)

    def create(
        self,
        *,
        customer_number: str,
        material_number: str,
        quantity: object,
        tax_price: object,
        order_date: str = "",
        note: str = "",
    ) -> SalesOrder:
        """创建一张暂存销售订单（状态 Z）。"""
        customer, customer_name = _resolve_customer(customer_number)
        material = _resolve_material(material_number)

        qty = _positive_decimal(quantity, "数量")
        price = _positive_decimal(tax_price, "单价")
        day = order_date.strip() or date.today().isoformat()
        amount = qty * price

        with self._lock:
            bill_no = self._next_bill_no_locked()
            fid = self._next_fid
            self._next_fid += 1
            order = SalesOrder(
                fid=fid,
                bill_no=bill_no,
                date=day,
                document_status="Z",
                close_status="A",
                bill_type_number=_DEFAULT_BILL_TYPE,
                customer_number=customer,
                customer_name=customer_name,
                sale_org_number=_DEFAULT_SALE_ORG,
                saler_name="demo",
                currency_number=_DEFAULT_CURRENCY,
                bill_all_amount=f"{amount:.{_AMOUNT_PRECISION}f}",
                create_date=day,
                note=note.strip(),
                entries=(
                    Entry(
                        material_number=material,
                        qty=f"{qty:.{_AMOUNT_PRECISION}f}",
                        tax_price=f"{price:.{_AMOUNT_PRECISION}f}",
                        delivery_date=day,
                    ),
                ),
            )
            self._orders[bill_no] = order
            return order

    def submit(self, bill_no: str) -> SalesOrder:
        """提交：Z/A → B。"""
        return self._transition(bill_no, {"Z", "A"}, "B", "提交")

    def audit(self, bill_no: str) -> SalesOrder:
        """审核：B → C。"""
        return self._transition(bill_no, {"B"}, "C", "审核")

    def unaudit(self, bill_no: str) -> SalesOrder:
        """反审核：C → A。"""
        return self._transition(bill_no, {"C"}, "A", "反审核")

    def delete(self, bill_no: str) -> None:
        """删除：仅允许 Z/A。"""
        with self._lock:
            order = self._orders.get(bill_no)
            if order is None:
                raise SalesOrderWriteError(f"订单不存在: {bill_no}")
            if order.document_status not in {"Z", "A"}:
                raise SalesOrderWriteError(
                    f"当前状态 {order.document_status} 不允许删除: {bill_no}"
                )
            del self._orders[bill_no]

    def _transition(
        self,
        bill_no: str,
        allowed: set[str],
        target: str,
        action_label: str,
    ) -> SalesOrder:
        with self._lock:
            order = self._orders.get(bill_no)
            if order is None:
                raise SalesOrderWriteError(f"订单不存在: {bill_no}")
            if order.document_status not in allowed:
                raise SalesOrderWriteError(
                    f"当前状态 {order.document_status} 不允许{action_label}: {bill_no}"
                )
            updated = replace(order, document_status=target)
            self._orders[bill_no] = updated
            return updated

    def _next_bill_no_locked(self) -> str:
        while True:
            bill_no = f"SO{self._next_seq:06d}"
            self._next_seq += 1
            if bill_no not in self._orders:
                return bill_no


def _resolve_customer(identifier: str) -> tuple[str, str]:
    """客户编码/FID → (编码, 名称)；不存在抛业务错误。"""
    text = identifier.strip().upper()
    number = _CUSTOMER_BY_FID.get(text, text)
    name = _CUSTOMER_BY_NUMBER.get(number)
    if name is None:
        raise SalesOrderWriteError(f"客户不存在: {identifier}")
    return number, name


def _resolve_material(identifier: str) -> str:
    """物料编码/FID → 编码；不存在抛业务错误。"""
    text = identifier.strip().upper()
    number = _MATERIAL_BY_FID.get(text, text)
    if number not in _MATERIAL_BY_NUMBER:
        raise SalesOrderWriteError(f"物料不存在: {identifier}")
    return number


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SalesOrderWriteError(f"{label}不是合法数字") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SalesOrderWriteError(f"{label}必须大于 0")
    return parsed


#: 进程内单例：查询引擎与写端点共享同一订单库
sales_order_store = SalesOrderStore()


def reset_dataset() -> None:
    """测试/演示复位订单库。"""
    sales_order_store.reset()
