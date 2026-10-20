"""沙箱可变采购订单库（采购订单下单写流程）。

职责：
- 以 ``dataset.PURCHASE_ORDERS`` 为种子，提供内存增删改查
- 固定状态机：Z/A → B → C → A；删除仅允许 Z/A
- 采购订单号 ``PO9xxxxx`` 递增，避开种子单号
- 业务校验：供应商/物料必须存在，数量/单价必须大于 0
- 线程锁保护并发写；``reset()`` 供测试 fixture 复位
"""

import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation

from sandbox.kingdee_stub.dataset import (
    MATERIAL_ROWS,
    PURCHASE_ORDERS,
    SUPPLIER_ROWS,
    PurchaseOrder,
)

_SUPPLIER_BY_NUMBER: frozenset[str] = frozenset(row.number for row in SUPPLIER_ROWS)
_SUPPLIER_BY_FID: dict[str, str] = {row.fid: row.number for row in SUPPLIER_ROWS}
_MATERIAL_BY_NUMBER: frozenset[str] = frozenset(row.number for row in MATERIAL_ROWS)
_MATERIAL_BY_FID: dict[str, str] = {row.fid: row.number for row in MATERIAL_ROWS}

_BILL_SEQ_START = 900_001
_AMOUNT_PRECISION = 10


class PurchaseOrderWriteError(Exception):
    """沙箱采购订单写操作业务错误（由 HTTP 层转成金蝶错误信封）。"""

    def __init__(self, message: str, *, code: int = 21) -> None:
        super().__init__(message)
        self.code = code


class PurchaseOrderStore:
    """进程内可变采购订单库。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders: dict[str, PurchaseOrder] = {}
        self._next_seq = _BILL_SEQ_START
        self.reset()

    def reset(self) -> None:
        """恢复为固定种子数据（测试/重启复位）。"""
        with self._lock:
            self._orders = {order.bill_no: order for order in PURCHASE_ORDERS}
            self._next_seq = _BILL_SEQ_START

    def orders(self) -> tuple[PurchaseOrder, ...]:
        """按单号升序返回当前订单快照。"""
        with self._lock:
            return tuple(sorted(self._orders.values(), key=lambda order: order.bill_no))

    def get(self, bill_no: str) -> PurchaseOrder | None:
        with self._lock:
            return self._orders.get(bill_no)

    def create(
        self,
        *,
        supplier_number: str,
        material_number: str,
        quantity: object,
        tax_price: object,
        order_date: str = "",
        note: str = "",
    ) -> PurchaseOrder:
        """创建一张暂存采购订单（状态 Z）。"""
        supplier = _resolve_supplier(supplier_number)
        material = _resolve_material(material_number)
        qty = _positive_decimal(quantity, "采购数量")
        price = _positive_decimal(tax_price, "含税单价")
        amount = qty * price
        day = order_date.strip() or date.today().isoformat()

        with self._lock:
            bill_no = self._next_bill_no_locked()
            order = PurchaseOrder(
                bill_no=bill_no,
                date=day,
                document_status="Z",
                supplier_number=supplier,
                supplier_name=_supplier_name(supplier),
                amount=f"{amount:.{_AMOUNT_PRECISION}f}",
                material_number=material,
                material_name=_material_name(material),
                qty=f"{qty:.{_AMOUNT_PRECISION}f}",
                tax_price=f"{price:.{_AMOUNT_PRECISION}f}",
                note=note.strip(),
            )
            self._orders[bill_no] = order
            return order

    def submit(self, bill_no: str) -> PurchaseOrder:
        """提交：Z/A → B。"""
        return self._transition(bill_no, {"Z", "A"}, "B", "提交")

    def audit(self, bill_no: str) -> PurchaseOrder:
        """审核：B → C。"""
        return self._transition(bill_no, {"B"}, "C", "审核")

    def unaudit(self, bill_no: str) -> PurchaseOrder:
        """反审核：C → A。"""
        return self._transition(bill_no, {"C"}, "A", "反审核")

    def delete(self, bill_no: str) -> None:
        """删除：仅允许 Z/A。"""
        with self._lock:
            order = self._orders.get(bill_no)
            if order is None:
                raise PurchaseOrderWriteError(f"采购订单不存在: {bill_no}")
            if order.document_status not in {"Z", "A"}:
                raise PurchaseOrderWriteError(
                    f"当前状态 {order.document_status} 不允许删除: {bill_no}"
                )
            del self._orders[bill_no]

    def _transition(
        self,
        bill_no: str,
        allowed: set[str],
        target: str,
        action_label: str,
    ) -> PurchaseOrder:
        with self._lock:
            order = self._orders.get(bill_no)
            if order is None:
                raise PurchaseOrderWriteError(f"采购订单不存在: {bill_no}")
            if order.document_status not in allowed:
                raise PurchaseOrderWriteError(
                    f"当前状态 {order.document_status} 不允许{action_label}: {bill_no}"
                )
            updated = replace(order, document_status=target)
            self._orders[bill_no] = updated
            return updated

    def _next_bill_no_locked(self) -> str:
        while True:
            bill_no = f"PO{self._next_seq:06d}"
            self._next_seq += 1
            if bill_no not in self._orders:
                return bill_no


def _resolve_supplier(identifier: str) -> str:
    text = identifier.strip().upper()
    number = _SUPPLIER_BY_FID.get(text, text)
    if number not in _SUPPLIER_BY_NUMBER:
        raise PurchaseOrderWriteError(f"供应商不存在: {identifier}")
    return number


def _resolve_material(identifier: str) -> str:
    text = identifier.strip().upper()
    number = _MATERIAL_BY_FID.get(text, text)
    if number not in _MATERIAL_BY_NUMBER:
        raise PurchaseOrderWriteError(f"物料不存在: {identifier}")
    return number


def _supplier_name(number: str) -> str:
    return next(row.name for row in SUPPLIER_ROWS if row.number == number)


def _material_name(number: str) -> str:
    return next(row.name for row in MATERIAL_ROWS if row.number == number)


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PurchaseOrderWriteError(f"{label}不是合法数字") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PurchaseOrderWriteError(f"{label}必须大于 0")
    return parsed


#: 进程内单例：查询引擎与写端点共享同一采购订单库
purchase_order_store = PurchaseOrderStore()


def reset_purchase_order_dataset() -> None:
    """测试/演示复位采购订单库。"""
    purchase_order_store.reset()
