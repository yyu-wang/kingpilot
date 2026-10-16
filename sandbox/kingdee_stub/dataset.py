"""仿真沙箱的销售订单种子数据集（M1-T06a）。

设计取舍：
- **固定种子程序化生成 130 条**：默认 ``Limit=100`` 时天然产生跨页（100 + 30），
  无需为「跨页」单独造数据；固定种子保证可重现（同一次运行内两次生成完全一致）。
- **前 8 条为具名手工样例**：让测试能断言精确值（大额、各状态、多明细），
  而不是只能断言「行数大于 0」。
- **金额一律用 10 位小数字符串**：与金蝶真实返回形态一致（``"2.0000000000"``），
  保持 T02 决策 D4「协议层不做精度转换」的一致立场。
- ``# INFERRED:`` 标注的取值域（关闭状态 A/B）未经真环境验证，待联调核对。
"""

import random
from collections.abc import Callable
from dataclasses import dataclass

from models.kingdee import JsonScalar

# ── 数据模型 ──


@dataclass(frozen=True)
class Entry:
    """销售订单明细行。"""

    material_number: str
    qty: str
    tax_price: str
    delivery_date: str


@dataclass(frozen=True)
class SalesOrder:
    """销售订单（表头 + 明细）。

    Why frozen：种子数据是不可变 fixture，禁止被测代码或测试之间互相篡改。
    """

    fid: int
    bill_no: str
    date: str
    document_status: str
    close_status: str
    bill_type_number: str
    customer_number: str
    customer_name: str
    sale_org_number: str
    saler_name: str
    currency_number: str
    bill_all_amount: str
    create_date: str
    note: str
    entries: tuple[Entry, ...]


# ── 字段取值访问器（字段名 → 取值函数）──
# Why 字典分发而非 if 链：字段名到取值的映射是纯数据关系，用表驱动更易审阅与扩展
_HEADER_ACCESSORS: dict[str, Callable[[SalesOrder], JsonScalar]] = {
    "FBillNo": lambda order: order.bill_no,
    "FID": lambda order: str(order.fid),
    "FDate": lambda order: order.date,
    "FDocumentStatus": lambda order: order.document_status,
    "FCloseStatus": lambda order: order.close_status,
    "FBillTypeID.FNumber": lambda order: order.bill_type_number,
    "FCustomerID.FName": lambda order: order.customer_name,
    "FCustomerID.FNumber": lambda order: order.customer_number,
    "FSaleOrgId.FNumber": lambda order: order.sale_org_number,
    "FSalerId.FName": lambda order: order.saler_name,
    "FCurrencyId.FNumber": lambda order: order.currency_number,
    "FBillAllAmount": lambda order: order.bill_all_amount,
    "FCreateDate": lambda order: order.create_date,
    "FNote": lambda order: order.note,
}

_ENTRY_ACCESSORS: dict[str, Callable[[Entry], JsonScalar]] = {
    "FMaterialId.FNumber": lambda entry: entry.material_number,
    "FQty": lambda entry: entry.qty,
    "FTaxPrice": lambda entry: entry.tax_price,
    "FDeliveryDate": lambda entry: entry.delivery_date,
}

ENTRY_FIELD_NAMES: frozenset[str] = frozenset(_ENTRY_ACCESSORS) | {"FEntryID"}


def header_value(order: SalesOrder, field_name: str) -> JsonScalar:
    """取表头字段值；字段名不在表头映射中返回 ``None``（上层已先做白名单校验）。"""
    accessor = _HEADER_ACCESSORS.get(field_name)
    return accessor(order) if accessor else None


def entry_value(order: SalesOrder, entry_index: int, field_name: str) -> JsonScalar:
    """取明细字段值（``FEntryID`` 为行号，非真实 ID）。"""
    if field_name == "FEntryID":
        return str(entry_index + 1)
    accessor = _ENTRY_ACCESSORS.get(field_name)
    if accessor is None or entry_index >= len(order.entries):
        return None
    return accessor(order.entries[entry_index])


def has_entry_field(field_keys: list[str]) -> bool:
    """请求字段里是否含明细字段——含则结果按明细行展开（一单多行）。"""
    return any(key in ENTRY_FIELD_NAMES for key in field_keys)


# ── 具名手工样例（测试断言的锚点）──

_NAMED_ORDERS: tuple[SalesOrder, ...] = (
    # 大额已审核单（对齐方案 §3.1 示例 FAmount > 100000）；第一张含 3 条明细
    SalesOrder(
        fid=1001,
        bill_no="SO000001",
        date="2026-01-05",
        document_status="C",
        close_status="A",
        bill_type_number="Standard",
        customer_number="CUS001",
        customer_name="测试客户001",
        sale_org_number="100",
        saler_name="张三",
        currency_number="PRE001",
        bill_all_amount="150000.0000000000",
        create_date="2026-01-05",
        note="大额已审核（3 条明细）",
        entries=(
            Entry("MAT001", "10.0000000000", "5000.0000000000", "2026-01-20"),
            Entry("MAT002", "20.0000000000", "3000.0000000000", "2026-01-21"),
            Entry("MAT003", "5.0000000000", "8000.0000000000", "2026-01-22"),
        ),
    ),
    SalesOrder(
        fid=1002,
        bill_no="SO000002",
        date="2026-01-06",
        document_status="C",
        close_status="A",
        bill_type_number="Standard",
        customer_number="CUS002",
        customer_name="测试客户002",
        sale_org_number="100",
        saler_name="李四",
        currency_number="PRE001",
        bill_all_amount="200000.0000000000",
        create_date="2026-01-06",
        note="大额已审核（2 条明细）",
        entries=(
            Entry("MAT001", "40.0000000000", "3000.0000000000", "2026-01-25"),
            Entry("MAT004", "8.0000000000", "10000.0000000000", "2026-01-26"),
        ),
    ),
    SalesOrder(
        fid=1003,
        bill_no="SO000003",
        date="2026-01-07",
        document_status="C",
        close_status="B",
        bill_type_number="Standard",
        customer_number="CUS003",
        customer_name="测试客户003",
        sale_org_number="101",
        saler_name="王五",
        currency_number="PRE002",
        bill_all_amount="120000.0000000000",
        create_date="2026-01-07",
        note="大额已审核（已关闭）",
        entries=(Entry("MAT002", "15.0000000000", "8000.0000000000", "2026-01-30"),),
    ),
    # 各状态覆盖（M8 评测的状态维度）
    SalesOrder(
        fid=1004,
        bill_no="SO000004",
        date="2026-02-01",
        document_status="Z",
        close_status="A",
        bill_type_number="Standard",
        customer_number="CUS001",
        customer_name="测试客户001",
        sale_org_number="100",
        saler_name="张三",
        currency_number="PRE001",
        bill_all_amount="1000.0000000000",
        create_date="2026-02-01",
        note="暂存单",
        entries=(Entry("MAT003", "1.0000000000", "1000.0000000000", "2026-02-10"),),
    ),
    SalesOrder(
        fid=1005,
        bill_no="SO000005",
        date="2026-02-02",
        document_status="A",
        close_status="A",
        bill_type_number="Standard",
        customer_number="CUS002",
        customer_name="测试客户002",
        sale_org_number="100",
        saler_name="李四",
        currency_number="PRE001",
        bill_all_amount="2500.0000000000",
        create_date="2026-02-02",
        note="创建单",
        entries=(Entry("MAT001", "5.0000000000", "500.0000000000", "2026-02-12"),),
    ),
    SalesOrder(
        fid=1006,
        bill_no="SO000006",
        date="2026-02-03",
        document_status="B",
        close_status="A",
        bill_type_number="Standard",
        customer_number="CUS003",
        customer_name="测试客户003",
        sale_org_number="101",
        saler_name="王五",
        currency_number="PRE001",
        bill_all_amount="3300.0000000000",
        create_date="2026-02-03",
        note="审核中单",
        entries=(Entry("MAT002", "3.0000000000", "1100.0000000000", "2026-02-15"),),
    ),
    SalesOrder(
        fid=1007,
        bill_no="SO000007",
        date="2026-02-04",
        document_status="D",
        close_status="A",
        bill_type_number="Standard",
        customer_number="CUS001",
        customer_name="测试客户001",
        sale_org_number="100",
        saler_name="张三",
        currency_number="PRE001",
        bill_all_amount="880.0000000000",
        create_date="2026-02-04",
        note="重新审核单",
        entries=(Entry("MAT003", "2.0000000000", "440.0000000000", "2026-02-18"),),
    ),
    SalesOrder(
        fid=1008,
        bill_no="SO000008",
        date="2026-02-05",
        document_status="C",
        close_status="A",
        bill_type_number="Standard",
        customer_number="CUS004",
        customer_name="测试客户004",
        sale_org_number="102",
        saler_name="赵六",
        currency_number="PRE002",
        bill_all_amount="45000.0000000000",
        create_date="2026-02-05",
        note="常规已审核单（大额阈值以下）",
        entries=(Entry("MAT005", "9.0000000000", "5000.0000000000", "2026-02-20"),),
    ),
)

_SEED = 20261016
_GENERATED_COUNT = 122  # 8 具名 + 122 = 130 条，超过默认 Limit=100 以覆盖跨页
_CUSTOMERS: tuple[tuple[str, str], ...] = (
    ("CUS001", "测试客户001"),
    ("CUS002", "测试客户002"),
    ("CUS003", "测试客户003"),
    ("CUS004", "测试客户004"),
    ("CUS005", "测试客户005"),
)
_STATUSES = ("Z", "A", "B", "C", "D")
_MATERIALS = ("MAT001", "MAT002", "MAT003", "MAT004", "MAT005")

_TOTAL_ORDERS = 130


def _generate_orders() -> tuple[SalesOrder, ...]:
    """固定种子生成其余订单（可重现；跨页与状态分布由其保证）。"""
    rng = random.Random(_SEED)
    orders: list[SalesOrder] = list(_NAMED_ORDERS)
    for index in range(len(_NAMED_ORDERS), _TOTAL_ORDERS):
        customer_number, customer_name = rng.choice(_CUSTOMERS)
        month = rng.randint(3, 12)
        day = rng.randint(1, 28)
        date = f"2026-{month:02d}-{day:02d}"
        entries = tuple(
            Entry(
                material_number=rng.choice(_MATERIALS),
                qty=f"{rng.randint(1, 99)}.0000000000",
                tax_price=f"{rng.randint(100, 20000)}.0000000000",
                delivery_date=date,
            )
            for _ in range(rng.randint(1, 3))
        )
        orders.append(
            SalesOrder(
                fid=1000 + index + 1,
                bill_no=f"SO{index + 1:06d}",
                date=date,
                # 已审核占多数，符合真实业务分布
                document_status=rng.choice(("C", "C", "C", "B", "A", "Z")),
                close_status=rng.choice(("A", "A", "B")),
                bill_type_number="Standard",
                customer_number=customer_number,
                customer_name=customer_name,
                sale_org_number=rng.choice(("100", "101", "102")),
                saler_name=rng.choice(("张三", "李四", "王五", "赵六")),
                currency_number=rng.choice(("PRE001", "PRE002")),
                bill_all_amount=f"{rng.randint(500, 300000)}.0000000000",
                create_date=date,
                note="",
                entries=entries,
            )
        )
    return tuple(orders)


# 按单据编号升序（默认排序 FBillNo 时的自然顺序）
ORDERS: tuple[SalesOrder, ...] = tuple(sorted(_generate_orders(), key=lambda order: order.bill_no))

# 供测试断言的状态分布参考：数据集必须覆盖 DocumentStatus 全部取值
STATUS_COVERAGE: frozenset[str] = frozenset(order.document_status for order in ORDERS)
ALL_STATUS_VALUES: frozenset[str] = frozenset(_STATUSES)


# ── 即时库存数据集（M2-T04）──
# Why 只支持 STK_Inventory 而非全部 5 端点：M2-T04 验收第 3 类问题是库存查询；
# 其余 3 个端点（customer/supplier/purchase_order）字段全部 INFERRED 未验证，
# 造数据=基于错误字段名造数据，等 T01 真凭证补验后再扩。


@dataclass(frozen=True)
class InventoryRow:
    """即时库存行（一物料一仓库一行，无明细展开概念）。"""

    material_number: str
    material_name: str
    stock_name: str
    base_qty: str  # 10 位小数字符串，与金蝶真实返回形态一致


_INVENTORY_ACCESSORS: dict[str, Callable[[InventoryRow], JsonScalar]] = {
    "FMaterialId.FNumber": lambda row: row.material_number,
    "FMaterialId.FName": lambda row: row.material_name,
    "FStockId.FName": lambda row: row.stock_name,
    "FBaseQty": lambda row: row.base_qty,
}


def inventory_value(row: InventoryRow, field_name: str) -> JsonScalar:
    """取库存行字段值；字段名不在映射中返回 ``None``（上层已先做白名单校验）。"""
    accessor = _INVENTORY_ACCESSORS.get(field_name)
    return accessor(row) if accessor else None


# 锚点：MAT001 分布在两个仓库（成品仓 100 + 原料仓 50），
# 集成用例「MAT001 这个物料的库存有多少」能验证多行聚合答复
INVENTORY_ROWS: tuple[InventoryRow, ...] = (
    InventoryRow("MAT001", "测试物料001", "成品仓", "100.0000000000"),
    InventoryRow("MAT001", "测试物料001", "原料仓", "50.0000000000"),
    InventoryRow("MAT002", "测试物料002", "成品仓", "30.0000000000"),
    InventoryRow("MAT003", "测试物料003", "成品仓", "8.0000000000"),
    InventoryRow("MAT004", "测试物料004", "半成品仓", "12.0000000000"),
    InventoryRow("MAT005", "测试物料005", "成品仓", "9.0000000000"),
)
