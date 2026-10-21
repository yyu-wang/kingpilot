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
    "FMaterialId.FName": lambda entry: _material_name(entry.material_number),
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
        # 月份上限截至 9 月：种子数据日期必须早于当天（10 月 20 日），
        # 否则新增单据（默认当天日期）会被未来日期数据淹没，难以查找
        month = rng.randint(3, 9)
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


# ── 基础资料（M9-T13：写操作实体预检数据源）──


@dataclass(frozen=True)
class CustomerRow:
    """客户基础资料行（BD_Customer 的仿真子集）。"""

    fid: str
    number: str
    name: str
    short_name: str


@dataclass(frozen=True)
class MaterialRow:
    """物料基础资料行（BD_Material 的仿真子集）。"""

    fid: str
    number: str
    name: str


# FID 取固定值：Guard 3 预检会把编码/名称换成 FID，写端点据此反查编码
CUSTOMER_ROWS: tuple[CustomerRow, ...] = (
    CustomerRow(fid="2001", number="CUS001", name="测试客户001", short_name="客户001"),
    CustomerRow(fid="2002", number="CUS002", name="测试客户002", short_name="客户002"),
    CustomerRow(fid="2003", number="CUS003", name="测试客户003", short_name="客户003"),
    CustomerRow(fid="2004", number="CUS004", name="测试客户004", short_name="客户004"),
    CustomerRow(fid="2005", number="CUS005", name="测试客户005", short_name="客户005"),
    CustomerRow(fid="2006", number="CUS006", name="华南贸易有限公司", short_name="华南贸易"),
    CustomerRow(fid="2007", number="CUS007", name="华北制造集团", short_name="华北制造"),
    CustomerRow(fid="2008", number="CUS008", name="华东零售连锁", short_name="华东零售"),
)

MATERIAL_ROWS: tuple[MaterialRow, ...] = (
    MaterialRow(fid="3001", number="MAT001", name="测试物料001"),
    MaterialRow(fid="3002", number="MAT002", name="测试物料002"),
    MaterialRow(fid="3003", number="MAT003", name="测试物料003"),
    MaterialRow(fid="3004", number="MAT004", name="测试物料004"),
    MaterialRow(fid="3005", number="MAT005", name="测试物料005"),
    MaterialRow(fid="3006", number="MAT006", name="测试物料006"),
    MaterialRow(fid="3007", number="MAT007", name="测试物料007"),
    MaterialRow(fid="3008", number="MAT008", name="测试物料008"),
)

# 编码 → 名称：销售订单明细只存编码，名称按基础资料反查（对齐真实金蝶 FMaterialId.FName）
_MATERIAL_NAMES: dict[str, str] = {row.number: row.name for row in MATERIAL_ROWS}


def _material_name(number: str) -> JsonScalar:
    """明细物料名称；未知编码返回 ``None``（与真实环境空值形态一致）。"""
    return _MATERIAL_NAMES.get(number)


_CUSTOMER_ACCESSORS: dict[str, Callable[[CustomerRow], JsonScalar]] = {
    "FID": lambda row: row.fid,
    "FNumber": lambda row: row.number,
    "FName": lambda row: row.name,
    "FShortName": lambda row: row.short_name,
}

_MATERIAL_ACCESSORS: dict[str, Callable[[MaterialRow], JsonScalar]] = {
    "FID": lambda row: row.fid,
    "FNumber": lambda row: row.number,
    "FName": lambda row: row.name,
}


def customer_value(row: CustomerRow, field_name: str) -> JsonScalar:
    """取客户字段值；未知字段返回 ``None``（上层已先做白名单校验）。"""
    accessor = _CUSTOMER_ACCESSORS.get(field_name)
    return accessor(row) if accessor else None


def material_value(row: MaterialRow, field_name: str) -> JsonScalar:
    """取物料字段值；未知字段返回 ``None``（上层已先做白名单校验）。"""
    accessor = _MATERIAL_ACCESSORS.get(field_name)
    return accessor(row) if accessor else None


# ── 供应商与采购订单（M9 测试数据补全）──


@dataclass(frozen=True)
class SupplierRow:
    """供应商基础资料行（BD_Supplier 的仿真子集）。"""

    fid: str
    number: str
    name: str
    short_name: str


@dataclass(frozen=True)
class PurchaseOrder:
    """采购订单（沙箱简化为单行：表头 + 一个物料行）。"""

    bill_no: str
    date: str
    document_status: str
    supplier_number: str
    supplier_name: str
    amount: str
    material_number: str = ""
    material_name: str = ""
    qty: str = "0.0000000000"
    tax_price: str = "0.0000000000"
    note: str = ""


SUPPLIER_ROWS: tuple[SupplierRow, ...] = (
    SupplierRow(fid="4001", number="SUP001", name="测试供应商001", short_name="供应商001"),
    SupplierRow(fid="4002", number="SUP002", name="测试供应商002", short_name="供应商002"),
    SupplierRow(fid="4003", number="SUP003", name="测试供应商003", short_name="供应商003"),
    SupplierRow(fid="4004", number="SUP004", name="深圳YY贸易有限公司", short_name="深圳YY贸易"),
    SupplierRow(fid="4005", number="SUP005", name="测试供应商005", short_name="供应商005"),
)

_SUPPLIER_ACCESSORS: dict[str, Callable[[SupplierRow], JsonScalar]] = {
    "FID": lambda row: row.fid,
    "FNumber": lambda row: row.number,
    "FName": lambda row: row.name,
    "FShortName": lambda row: row.short_name,
}

_PURCHASE_ORDER_ACCESSORS: dict[str, Callable[[PurchaseOrder], JsonScalar]] = {
    "FBillNo": lambda order: order.bill_no,
    "FDate": lambda order: order.date,
    "FDocumentStatus": lambda order: order.document_status,
    "FSupplierID.FNumber": lambda order: order.supplier_number,
    "FSupplierID.FName": lambda order: order.supplier_name,
    "FAmount": lambda order: order.amount,
    "FMaterialId.FNumber": lambda order: order.material_number,
    "FMaterialId.FName": lambda order: order.material_name,
    "FQty": lambda order: order.qty,
    "FTaxPrice": lambda order: order.tax_price,
}


def supplier_value(row: SupplierRow, field_name: str) -> JsonScalar:
    """取供应商字段值；未知字段返回 ``None``（上层已先做白名单校验）。"""
    accessor = _SUPPLIER_ACCESSORS.get(field_name)
    return accessor(row) if accessor else None


def purchase_order_value(order: PurchaseOrder, field_name: str) -> JsonScalar:
    """取采购订单字段值；未知字段返回 ``None``（上层已先做白名单校验）。"""
    accessor = _PURCHASE_ORDER_ACCESSORS.get(field_name)
    return accessor(order) if accessor else None


# 具名锚点：覆盖全部单据状态，便于断言精确值
_NAMED_PURCHASE_ORDERS: tuple[PurchaseOrder, ...] = (
    PurchaseOrder(
        "PO000001",
        "2026-01-03",
        "C",
        "SUP001",
        "测试供应商001",
        "88000.0000000000",
        "MAT001",
        "测试物料001",
        "88.0000000000",
        "1000.0000000000",
    ),
    PurchaseOrder(
        "PO000002",
        "2026-01-08",
        "C",
        "SUP002",
        "测试供应商002",
        "160000.0000000000",
        "MAT002",
        "测试物料002",
        "160.0000000000",
        "1000.0000000000",
    ),
    PurchaseOrder(
        "PO000003",
        "2026-01-12",
        "B",
        "SUP003",
        "测试供应商003",
        "25000.0000000000",
        "MAT003",
        "测试物料003",
        "25.0000000000",
        "1000.0000000000",
    ),
    PurchaseOrder(
        "PO000004",
        "2026-02-01",
        "Z",
        "SUP001",
        "测试供应商001",
        "5000.0000000000",
        "MAT004",
        "测试物料004",
        "5.0000000000",
        "1000.0000000000",
    ),
    PurchaseOrder(
        "PO000005",
        "2026-02-02",
        "A",
        "SUP002",
        "测试供应商002",
        "12000.0000000000",
        "MAT005",
        "测试物料005",
        "12.0000000000",
        "1000.0000000000",
    ),
    PurchaseOrder(
        "PO000006",
        "2026-02-03",
        "D",
        "SUP004",
        "深圳YY贸易有限公司",
        "3000.0000000000",
        "MAT006",
        "测试物料006",
        "3.0000000000",
        "1000.0000000000",
    ),
    PurchaseOrder(
        "PO000007",
        "2026-03-05",
        "C",
        "SUP005",
        "测试供应商005",
        "45000.0000000000",
        "MAT006",
        "测试物料006",
        "45.0000000000",
        "1000.0000000000",
    ),
    PurchaseOrder(
        "PO000008",
        "2026-03-06",
        "C",
        "SUP001",
        "测试供应商001",
        "9900.0000000000",
        "MAT008",
        "测试物料008",
        "9.0000000000",
        "1100.0000000000",
    ),
)

_PO_SEED = 20261019
_PO_TOTAL = 40


def _generate_purchase_orders() -> tuple[PurchaseOrder, ...]:
    """固定种子生成其余采购订单（状态/供应商/物料分布可重现）。"""
    rng = random.Random(_PO_SEED)
    orders: list[PurchaseOrder] = list(_NAMED_PURCHASE_ORDERS)
    suppliers = tuple((row.number, row.name) for row in SUPPLIER_ROWS)
    materials = tuple((row.number, row.name) for row in MATERIAL_ROWS)
    for index in range(len(_NAMED_PURCHASE_ORDERS), _PO_TOTAL):
        supplier_number, supplier_name = rng.choice(suppliers)
        material_number, material_name = rng.choice(materials)
        qty = rng.randint(1, 100)
        price = rng.randint(100, 2000)
        # 同上：采购订单日期也限制在 9 月及以前，保证全部早于当天
        month = rng.randint(4, 9)
        day = rng.randint(1, 28)
        orders.append(
            PurchaseOrder(
                bill_no=f"PO{index + 1:06d}",
                date=f"2026-{month:02d}-{day:02d}",
                document_status=rng.choice(("C", "C", "B", "A", "Z", "D")),
                supplier_number=supplier_number,
                supplier_name=supplier_name,
                amount=f"{qty * price}.0000000000",
                material_number=material_number,
                material_name=material_name,
                qty=f"{qty}.0000000000",
                tax_price=f"{price}.0000000000",
            )
        )
    return tuple(orders)


PURCHASE_ORDERS: tuple[PurchaseOrder, ...] = tuple(
    sorted(_generate_purchase_orders(), key=lambda order: order.bill_no)
)
PURCHASE_STATUS_COVERAGE: frozenset[str] = frozenset(
    order.document_status for order in PURCHASE_ORDERS
)


# ── 采购入库单（采购入库流程测试数据）──


@dataclass(frozen=True)
class PurchaseInboundRow:
    """采购入库单行（STK_InStock 的仿真子集）。

    ``source_bill_no`` 回链采购订单，用于验证「订单 → 到货入库」流程。
    """

    bill_no: str
    date: str
    document_status: str
    supplier_number: str
    supplier_name: str
    warehouse_name: str
    material_number: str
    material_name: str
    real_qty: str
    source_bill_no: str
    amount: str
    note: str = ""


_NAMED_PURCHASE_INBOUND_ROWS: tuple[PurchaseInboundRow, ...] = (
    PurchaseInboundRow(
        "CGRK000001",
        "2026-01-06",
        "C",
        "SUP001",
        "测试供应商001",
        "成品仓",
        "MAT001",
        "测试物料001",
        "50.0000000000",
        "PO000001",
        "44000.0000000000",
    ),
    PurchaseInboundRow(
        "CGRK000002",
        "2026-01-10",
        "C",
        "SUP002",
        "测试供应商002",
        "原料仓",
        "MAT002",
        "测试物料002",
        "30.0000000000",
        "PO000002",
        "48000.0000000000",
    ),
    PurchaseInboundRow(
        "CGRK000003",
        "2026-01-15",
        "B",
        "SUP003",
        "测试供应商003",
        "成品仓",
        "MAT003",
        "测试物料003",
        "10.0000000000",
        "PO000003",
        "8000.0000000000",
    ),
    PurchaseInboundRow(
        "CGRK000004",
        "2026-02-05",
        "Z",
        "SUP001",
        "测试供应商001",
        "成品仓",
        "MAT004",
        "测试物料004",
        "5.0000000000",
        "PO000004",
        "2500.0000000000",
    ),
    PurchaseInboundRow(
        "CGRK000005",
        "2026-02-06",
        "A",
        "SUP002",
        "测试供应商002",
        "半成品仓",
        "MAT005",
        "测试物料005",
        "8.0000000000",
        "PO000005",
        "3200.0000000000",
    ),
    PurchaseInboundRow(
        "CGRK000006",
        "2026-02-10",
        "C",
        "SUP004",
        "深圳YY贸易有限公司",
        "原料仓",
        "MAT006",
        "测试物料006",
        "20.0000000000",
        "PO000006",
        "6000.0000000000",
    ),
    PurchaseInboundRow(
        "CGRK000007",
        "2026-03-08",
        "D",
        "SUP005",
        "测试供应商005",
        "成品仓",
        "MAT006",
        "测试物料006",
        "15.0000000000",
        "PO000007",
        "7500.0000000000",
    ),
    PurchaseInboundRow(
        "CGRK000008",
        "2026-03-09",
        "C",
        "SUP001",
        "测试供应商001",
        "原料仓",
        "MAT008",
        "测试物料008",
        "12.0000000000",
        "PO000008",
        "4800.0000000000",
    ),
)

_INBOUND_SEED = 20261020
_INBOUND_TOTAL = 14


def _generate_purchase_inbounds() -> tuple[PurchaseInboundRow, ...]:
    """固定种子补足入库单，保证源采购订单都存在且状态分布可重现。"""
    rng = random.Random(_INBOUND_SEED)
    rows: list[PurchaseInboundRow] = list(_NAMED_PURCHASE_INBOUND_ROWS)
    materials = tuple((row.number, row.name) for row in MATERIAL_ROWS)
    warehouses = ("成品仓", "原料仓", "半成品仓")
    # 从第 9 张采购订单开始取源单，避免与具名锚点重复
    source_orders = PURCHASE_ORDERS[8:]
    for index in range(len(_NAMED_PURCHASE_INBOUND_ROWS), _INBOUND_TOTAL):
        source = source_orders[index - len(_NAMED_PURCHASE_INBOUND_ROWS)]
        material_number, material_name = rng.choice(materials)
        rows.append(
            PurchaseInboundRow(
                bill_no=f"CGRK{index + 1:06d}",
                date=source.date,
                document_status=rng.choice(("C", "C", "B", "A", "Z", "D")),
                supplier_number=source.supplier_number,
                supplier_name=source.supplier_name,
                warehouse_name=rng.choice(warehouses),
                material_number=material_number,
                material_name=material_name,
                real_qty=f"{rng.randint(1, 100)}.0000000000",
                source_bill_no=source.bill_no,
                amount=f"{rng.randint(1000, 90000)}.0000000000",
            )
        )
    return tuple(rows)


PURCHASE_INBOUND_ROWS: tuple[PurchaseInboundRow, ...] = tuple(
    sorted(_generate_purchase_inbounds(), key=lambda row: row.bill_no)
)
PURCHASE_INBOUND_STATUS_COVERAGE: frozenset[str] = frozenset(
    row.document_status for row in PURCHASE_INBOUND_ROWS
)

_PURCHASE_INBOUND_ACCESSORS: dict[str, Callable[[PurchaseInboundRow], JsonScalar]] = {
    "FBillNo": lambda row: row.bill_no,
    "FDate": lambda row: row.date,
    "FDocumentStatus": lambda row: row.document_status,
    "FSupplierID.FNumber": lambda row: row.supplier_number,
    "FSupplierID.FName": lambda row: row.supplier_name,
    "FStockId.FName": lambda row: row.warehouse_name,
    "FMaterialId.FNumber": lambda row: row.material_number,
    "FMaterialId.FName": lambda row: row.material_name,
    "FRealQty": lambda row: row.real_qty,
    "FSrcBillNo": lambda row: row.source_bill_no,
    "FAmount": lambda row: row.amount,
}


def purchase_inbound_value(row: PurchaseInboundRow, field_name: str) -> JsonScalar:
    """取采购入库单字段值；未知字段返回 ``None``（上层已先做白名单校验）。"""
    accessor = _PURCHASE_INBOUND_ACCESSORS.get(field_name)
    return accessor(row) if accessor else None


# ── 即时库存数据集（M2-T04）──
# 字段拼写仍为 INFERRED；M9 测试数据补全后已覆盖销售/采购/客户/供应商/库存五个端点。


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
    InventoryRow("MAT006", "测试物料006", "成品仓", "40.0000000000"),
    # 零库存物料：验证「物料存在但无库存」与「物料不存在」是两种不同答复
    InventoryRow("MAT007", "测试物料007", "原料仓", "0.0000000000"),
    InventoryRow("MAT008", "测试物料008", "半成品仓", "25.0000000000"),
)
