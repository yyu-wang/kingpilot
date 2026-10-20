"""ExecuteBillQuery 查询引擎（M2-T04 从 app.py 拆出）。

按 FormId 注册表分发业务对象：字段白名单、字段类型表、行构造各归各对象，
过滤/排序/投影逻辑对所有 FormId 通用——「加一个端点」= 注册一项。

Why 单独成模块：HTTP 层（app.py）与查询语义（本模块）变化原因不同；
且 app.py 曾逼近 400 行上限（§3.2 结构规范）。
"""

import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from kingdee.endpoints.base import EndpointDescriptor
from kingdee.endpoints.bd_customer import CUSTOMER
from kingdee.endpoints.bd_material import BD_MATERIAL
from kingdee.endpoints.bd_supplier import SUPPLIER
from kingdee.endpoints.pur_order import PURCHASE_ORDER
from kingdee.endpoints.sal_order import SAL_ORDER
from kingdee.endpoints.stk_inbound import PURCHASE_INBOUND
from kingdee.endpoints.stk_inventory import INVENTORY
from models.kingdee import JsonScalar
from sandbox.kingdee_stub.dataset import (
    CUSTOMER_ROWS,
    ENTRY_FIELD_NAMES,
    INVENTORY_ROWS,
    MATERIAL_ROWS,
    SUPPLIER_ROWS,
    SalesOrder,
    customer_value,
    entry_value,
    has_entry_field,
    header_value,
    inventory_value,
    material_value,
    purchase_inbound_value,
    purchase_order_value,
    supplier_value,
)
from sandbox.kingdee_stub.filter_parser import FilterSyntaxError
from sandbox.kingdee_stub.inbound_store import purchase_inbound_store
from sandbox.kingdee_stub.purchase_order_store import purchase_order_store
from sandbox.kingdee_stub.store import sales_order_store

# ── 行构造 ──


def _sal_full_row(order: SalesOrder, entry_index: int | None) -> dict[str, JsonScalar]:
    """构造销售订单「全字段」行（含未在 FieldKeys 里的字段，供过滤/排序取值用）。

    Why 建全字段行而非只建请求列：真金蝶允许过滤字段不出现在 FieldKeys 里
    （如按 ``FCustomerID.FNumber`` 过滤却只显示 ``FBillNo``）。若只为请求列建行，
    这类过滤会静默失效——正是契约测试最该拦住的假绿。
    """
    row: dict[str, JsonScalar] = {
        name: header_value(order, name) for name in SAL_ORDER.selectable_fields - ENTRY_FIELD_NAMES
    }
    for name in ENTRY_FIELD_NAMES:
        row[name] = entry_value(order, entry_index, name) if entry_index is not None else None
    return row


def _build_sal_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """销售订单行构造：FieldKeys 含明细字段时一单多行展开。"""
    rows: list[dict[str, JsonScalar]] = []
    expand_entries = has_entry_field(field_keys)
    for order in sales_order_store.orders():
        if expand_entries:
            rows.extend(_sal_full_row(order, index) for index in range(len(order.entries)))
        else:
            rows.append(_sal_full_row(order, None))
    return rows


def _build_inventory_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """库存行构造：一物料一仓库一行，无明细展开（field_keys 仅为签名对齐）。"""
    return [
        {name: inventory_value(row, name) for name in INVENTORY.selectable_fields}
        for row in INVENTORY_ROWS
    ]


def _build_customer_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """客户行构造（M9-T13 实体预检数据源）。"""
    return [
        {name: customer_value(row, name) for name in CUSTOMER.selectable_fields}
        for row in CUSTOMER_ROWS
    ]


def _build_material_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """物料行构造（M9-T13 实体预检数据源）。"""
    return [
        {name: material_value(row, name) for name in BD_MATERIAL.selectable_fields}
        for row in MATERIAL_ROWS
    ]


def _build_supplier_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """供应商行构造（采购订单查询与实体预检数据源）。"""
    return [
        {name: supplier_value(row, name) for name in SUPPLIER.selectable_fields}
        for row in SUPPLIER_ROWS
    ]


def _build_purchase_order_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """采购订单行构造（读可变库，下单/删除后查询立即可见）。"""
    return [
        {name: purchase_order_value(order, name) for name in PURCHASE_ORDER.selectable_fields}
        for order in purchase_order_store.orders()
    ]


def _build_purchase_inbound_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """采购入库单行构造（读可变库，新建/删除后查询立即可见）。"""
    return [
        {name: purchase_inbound_value(row, name) for name in PURCHASE_INBOUND.selectable_fields}
        for row in purchase_inbound_store.rows()
    ]


# ── FormId 注册表 ──


@dataclass(frozen=True)
class FormModel:
    """单个业务对象的仿真查询模型：字段白名单、类型表与行构造器。"""

    descriptor: EndpointDescriptor
    field_types: dict[str, str]
    non_sortable: frozenset[str]  # 存在但不可排序的字段（销售订单明细行无稳定语义）
    build_rows: Callable[[list[str]], list[dict[str, JsonScalar]]]


FORMS: dict[str, FormModel] = {
    SAL_ORDER.form_id: FormModel(
        descriptor=SAL_ORDER,
        field_types={field.name: field.data_type for field in SAL_ORDER.fields},
        non_sortable=ENTRY_FIELD_NAMES,
        build_rows=_build_sal_rows,
    ),
    INVENTORY.form_id: FormModel(
        descriptor=INVENTORY,
        field_types={field.name: field.data_type for field in INVENTORY.fields},
        non_sortable=frozenset(),
        build_rows=_build_inventory_rows,
    ),
    CUSTOMER.form_id: FormModel(
        descriptor=CUSTOMER,
        field_types={field.name: field.data_type for field in CUSTOMER.fields},
        non_sortable=frozenset({"FID"}),
        build_rows=_build_customer_rows,
    ),
    BD_MATERIAL.form_id: FormModel(
        descriptor=BD_MATERIAL,
        field_types={field.name: field.data_type for field in BD_MATERIAL.fields},
        non_sortable=frozenset({"FID"}),
        build_rows=_build_material_rows,
    ),
    SUPPLIER.form_id: FormModel(
        descriptor=SUPPLIER,
        field_types={field.name: field.data_type for field in SUPPLIER.fields},
        non_sortable=frozenset({"FID"}),
        build_rows=_build_supplier_rows,
    ),
    PURCHASE_ORDER.form_id: FormModel(
        descriptor=PURCHASE_ORDER,
        field_types={field.name: field.data_type for field in PURCHASE_ORDER.fields},
        non_sortable=frozenset(),
        build_rows=_build_purchase_order_rows,
    ),
    PURCHASE_INBOUND.form_id: FormModel(
        descriptor=PURCHASE_INBOUND,
        field_types={field.name: field.data_type for field in PURCHASE_INBOUND.fields},
        non_sortable=frozenset(),
        build_rows=_build_purchase_inbound_rows,
    ),
}


# ── 过滤求值 ──


def _as_decimal(value: JsonScalar) -> Decimal | None:
    """把单元格值转为 Decimal（数值比较用，避免 float 精度误差）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _match_like(actual: str, pattern: str) -> bool:
    """SQL 通配匹配：``%`` 任意串、``_`` 单字符（其余字符按字面量）。"""
    regex = "".join(
        ".*" if char == "%" else "." if char == "_" else re.escape(char) for char in pattern
    )
    return re.fullmatch(regex, actual) is not None


# 比较符 → 运算函数（表驱动，避免 6 个 if 分支在数值/字符串两处重复）
_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq,
    "<>": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def _match(actual: JsonScalar, op: str, expected: str, data_type: str) -> bool:
    """单个条件求值；``actual`` 为 ``None`` 时实例值缺失，一律不匹配。"""
    if actual is None:
        return False
    if op == "LIKE":
        return _match_like(str(actual), expected)
    compare = _COMPARATORS[op]
    if data_type == "number":
        # 数值字段两侧都转 Decimal 再比（字符串直接比会得到 "9" > "10" 的错误结论）
        left, right = _as_decimal(actual), _as_decimal(expected)
        if left is None or right is None:
            return False
        return compare(left, right)
    return compare(str(actual), expected)


def apply_filter(
    rows: list[dict[str, JsonScalar]],
    conditions: tuple[Any, ...],
    field_types: dict[str, str],
) -> list[dict[str, JsonScalar]]:
    """逐条件过滤（AND 语义；条件间相互收敛）。"""
    for condition in conditions:
        data_type = field_types.get(condition.field, "string")
        rows = [
            row
            for row in rows
            if _match(row.get(condition.field), condition.op, condition.value, data_type)
        ]
    return rows


# ── 排序 ──


def parse_order_string(
    order_string: str, selectable: frozenset[str], non_sortable: frozenset[str]
) -> tuple[tuple[str, bool], ...]:
    """解析 ``OrderString``（如 ``"FBillNo"`` / ``"FDate DESC,FBillNo"``）。

    Raises:
        FilterSyntaxError: 排序字段不可排序、字段不在白名单、或方向不是 ASC/DESC。
    """
    parsed: list[tuple[str, bool]] = []
    for raw in order_string.split(","):
        tokens = raw.strip().split()
        if not tokens:
            continue
        field = tokens[0]
        if field in non_sortable:
            raise FilterSyntaxError(f"仿真端不支持按该字段排序: {field!r}")
        if field not in selectable:
            raise FilterSyntaxError(f"排序字段不在白名单: {field!r}")
        direction = tokens[1].upper() if len(tokens) > 1 else "ASC"
        if direction not in ("ASC", "DESC"):
            raise FilterSyntaxError(f"排序方向非法: {raw!r}")
        parsed.append((field, direction == "DESC"))
    return tuple(parsed)


def _sort_key(row: dict[str, JsonScalar], field: str, field_types: dict[str, str]) -> str | Decimal:
    """排序键：数值字段用 Decimal，其余用字符串（ISO 日期字符串序即时间序）。"""
    value = row.get(field)
    if value is None:
        return ""
    if field_types.get(field) == "number":
        return _as_decimal(value) or Decimal(0)
    return str(value)


def _make_sort_key(
    field: str, field_types: dict[str, str]
) -> Callable[[dict[str, JsonScalar]], str | Decimal]:
    """生成「按指定字段取排序键」的函数（供 ``list.sort(key=...)`` 用）。

    Why 不直接写 lambda：``lambda row, f=field: ...`` 带默认参数时 mypy 无法推断
    参数类型（strict 模式报 ``Cannot infer type of lambda``）；工厂函数可显式标注。
    """

    def key(row: dict[str, JsonScalar]) -> str | Decimal:
        return _sort_key(row, field, field_types)

    return key


def apply_order(
    rows: list[dict[str, JsonScalar]],
    keys: tuple[tuple[str, bool], ...],
    field_types: dict[str, str],
) -> list[dict[str, JsonScalar]]:
    """多键排序：按「逆序稳定排序」逐个应用，天然支持混合升降序。"""
    for field, descending in reversed(keys):
        rows.sort(key=_make_sort_key(field, field_types), reverse=descending)
    return rows
