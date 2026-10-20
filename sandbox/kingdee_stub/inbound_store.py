"""沙箱可变采购入库单库（采购入库写流程）。

职责：
- 以 ``dataset.PURCHASE_INBOUND_ROWS`` 为种子，提供内存增删改查
- 固定状态机：Z/A → B → C → A；删除仅允许 Z/A
- 入库单号 ``CGRK9xxxxx`` 递增，避开种子单号
- 业务校验：源采购订单必须存在且已审核、供应商一致、物料/仓库存在
- 线程锁保护并发写；``reset()`` 供测试 fixture 复位
"""

import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation

from sandbox.kingdee_stub.dataset import (
    INVENTORY_ROWS,
    MATERIAL_ROWS,
    PURCHASE_INBOUND_ROWS,
    SUPPLIER_ROWS,
    PurchaseInboundRow,
)
from sandbox.kingdee_stub.purchase_order_store import purchase_order_store

_SUPPLIER_BY_NUMBER: frozenset[str] = frozenset(row.number for row in SUPPLIER_ROWS)
_SUPPLIER_BY_FID: dict[str, str] = {row.fid: row.number for row in SUPPLIER_ROWS}
_MATERIAL_BY_NUMBER: frozenset[str] = frozenset(row.number for row in MATERIAL_ROWS)
_MATERIAL_BY_FID: dict[str, str] = {row.fid: row.number for row in MATERIAL_ROWS}
_WAREHOUSES: frozenset[str] = frozenset(row.stock_name for row in INVENTORY_ROWS)

_BILL_SEQ_START = 900_001
_AMOUNT_PRECISION = 10


class PurchaseInboundWriteError(Exception):
    """沙箱采购入库写操作业务错误（由 HTTP 层转成金蝶错误信封）。"""

    def __init__(self, message: str, *, code: int = 21) -> None:
        super().__init__(message)
        self.code = code


class PurchaseInboundStore:
    """进程内可变采购入库单库。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, PurchaseInboundRow] = {}
        self._next_seq = _BILL_SEQ_START
        self.reset()

    def reset(self) -> None:
        """恢复为固定种子数据（测试/重启复位）。"""
        with self._lock:
            self._rows = {row.bill_no: row for row in PURCHASE_INBOUND_ROWS}
            self._next_seq = _BILL_SEQ_START

    def rows(self) -> tuple[PurchaseInboundRow, ...]:
        """按入库单号升序返回当前数据快照。"""
        with self._lock:
            return tuple(sorted(self._rows.values(), key=lambda row: row.bill_no))

    def get(self, bill_no: str) -> PurchaseInboundRow | None:
        with self._lock:
            return self._rows.get(bill_no)

    def create(
        self,
        *,
        source_bill_no: str,
        supplier_number: str,
        material_number: str,
        warehouse_name: str,
        quantity: object,
        tax_price: object = None,
        inbound_date: str = "",
        note: str = "",
    ) -> PurchaseInboundRow:
        """创建一张暂存采购入库单（状态 Z）。"""
        source = purchase_order_store.get(source_bill_no.strip())
        if source is None:
            raise PurchaseInboundWriteError(f"源采购订单不存在: {source_bill_no}")
        if source.document_status != "C":
            raise PurchaseInboundWriteError(f"源采购订单未审核，不能入库: {source_bill_no}")

        supplier = _resolve_supplier(supplier_number)
        if supplier != source.supplier_number:
            raise PurchaseInboundWriteError(
                f"供应商与源采购订单不一致: {supplier_number} != {source.supplier_number}"
            )
        material = _resolve_material(material_number)
        warehouse = warehouse_name.strip()
        if warehouse not in _WAREHOUSES:
            raise PurchaseInboundWriteError(f"仓库不存在: {warehouse_name}")

        qty = _positive_decimal(quantity, "实收数量")
        price = _positive_decimal(tax_price, "单价") if tax_price is not None else Decimal(0)
        amount = qty * price
        day = inbound_date.strip() or date.today().isoformat()

        with self._lock:
            bill_no = self._next_bill_no_locked()
            row = PurchaseInboundRow(
                bill_no=bill_no,
                date=day,
                document_status="Z",
                supplier_number=supplier,
                supplier_name=_supplier_name(supplier),
                warehouse_name=warehouse,
                material_number=material,
                material_name=_material_name(material),
                real_qty=f"{qty:.{_AMOUNT_PRECISION}f}",
                source_bill_no=source_bill_no.strip(),
                amount=f"{amount:.{_AMOUNT_PRECISION}f}",
                note=note.strip(),
            )
            self._rows[bill_no] = row
            return row

    def submit(self, bill_no: str) -> PurchaseInboundRow:
        """提交：Z/A → B。"""
        return self._transition(bill_no, {"Z", "A"}, "B", "提交")

    def audit(self, bill_no: str) -> PurchaseInboundRow:
        """审核：B → C。"""
        return self._transition(bill_no, {"B"}, "C", "审核")

    def unaudit(self, bill_no: str) -> PurchaseInboundRow:
        """反审核：C → A。"""
        return self._transition(bill_no, {"C"}, "A", "反审核")

    def delete(self, bill_no: str) -> None:
        """删除：仅允许 Z/A。"""
        with self._lock:
            row = self._rows.get(bill_no)
            if row is None:
                raise PurchaseInboundWriteError(f"采购入库单不存在: {bill_no}")
            if row.document_status not in {"Z", "A"}:
                raise PurchaseInboundWriteError(
                    f"当前状态 {row.document_status} 不允许删除: {bill_no}"
                )
            del self._rows[bill_no]

    def _transition(
        self,
        bill_no: str,
        allowed: set[str],
        target: str,
        action_label: str,
    ) -> PurchaseInboundRow:
        with self._lock:
            row = self._rows.get(bill_no)
            if row is None:
                raise PurchaseInboundWriteError(f"采购入库单不存在: {bill_no}")
            if row.document_status not in allowed:
                raise PurchaseInboundWriteError(
                    f"当前状态 {row.document_status} 不允许{action_label}: {bill_no}"
                )
            updated = replace(row, document_status=target)
            self._rows[bill_no] = updated
            return updated

    def _next_bill_no_locked(self) -> str:
        while True:
            bill_no = f"CGRK{self._next_seq:06d}"
            self._next_seq += 1
            if bill_no not in self._rows:
                return bill_no


def _resolve_supplier(identifier: str) -> str:
    text = identifier.strip().upper()
    number = _SUPPLIER_BY_FID.get(text, text)
    if number not in _SUPPLIER_BY_NUMBER:
        raise PurchaseInboundWriteError(f"供应商不存在: {identifier}")
    return number


def _resolve_material(identifier: str) -> str:
    text = identifier.strip().upper()
    number = _MATERIAL_BY_FID.get(text, text)
    if number not in _MATERIAL_BY_NUMBER:
        raise PurchaseInboundWriteError(f"物料不存在: {identifier}")
    return number


def _supplier_name(number: str) -> str:
    return next(row.name for row in SUPPLIER_ROWS if row.number == number)


def _material_name(number: str) -> str:
    return next(row.name for row in MATERIAL_ROWS if row.number == number)


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PurchaseInboundWriteError(f"{label}不是合法数字") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise PurchaseInboundWriteError(f"{label}必须大于 0")
    return parsed


#: 进程内单例：查询引擎与写端点共享同一入库单库
purchase_inbound_store = PurchaseInboundStore()


def reset_purchase_inbound_dataset() -> None:
    """测试/演示复位采购入库单库。"""
    purchase_inbound_store.reset()
