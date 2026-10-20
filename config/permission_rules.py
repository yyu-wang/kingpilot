"""M4-T08 · 权限规则表（方案 §5.1 支柱五 + 数据域读写隔离）。

运行时**只读**。本文件只声明策略，判定逻辑在 ``safety/permission_engine.py``。

四层权限的配置来源：
1. 工具层：``UserContext.allow_tools / deny_tools``（用户上下文承载）
2. 数据域：``FORM_DOMAIN_MAP`` + ``ROLE_DOMAIN_MATRIX``
3. 行级：``data_scope.org_ids / owner_only`` + ``ROW_*_FIELD_MAP``
4. 列级：``COLUMN_DENY_MAP``
写规则：``WRITE_RULES``（金额升级 / 禁止删除）
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class DataDomain(StrEnum):
    """业务数据域（与方案 §5.1 扩展一致）。"""

    SALES = "sales"
    PURCHASE = "purchase"
    INVENTORY = "inventory"
    FINANCE = "finance"
    HR = "hr"
    PRODUCTION = "production"


_ALL_DOMAINS = frozenset(DataDomain)

# 表单 → 数据域；未登记的表单一律 fail-closed 拒绝
FORM_DOMAIN_MAP: dict[str, DataDomain] = {
    "SAL_Order": DataDomain.SALES,
    "BD_Customer": DataDomain.SALES,
    "PUR_PurchaseOrder": DataDomain.PURCHASE,
    "BD_Supplier": DataDomain.PURCHASE,
    "STK_Inventory": DataDomain.INVENTORY,
    "STK_InStock": DataDomain.INVENTORY,  # 采购入库单
    "GL_Voucher": DataDomain.FINANCE,
    "AP_Bill": DataDomain.FINANCE,
    "AR_Bill": DataDomain.FINANCE,
    "BAS_Employee": DataDomain.HR,
    "PRD_Order": DataDomain.PRODUCTION,
    "PRD_Report": DataDomain.PRODUCTION,
}


@dataclass(frozen=True)
class DomainAccess:
    """角色在数据域上的读写权限。"""

    visible: frozenset[DataDomain]
    writable: frozenset[DataDomain]


# 角色 → 可见/可写域（可见 ≠ 可写：财务可看销售采购用于对账，但只写财务）
ROLE_DOMAIN_MATRIX: dict[str, DomainAccess] = {
    "admin": DomainAccess(visible=_ALL_DOMAINS, writable=_ALL_DOMAINS),
    "manager": DomainAccess(visible=_ALL_DOMAINS, writable=_ALL_DOMAINS),
    "operator": DomainAccess(
        visible=frozenset({DataDomain.SALES, DataDomain.PURCHASE, DataDomain.INVENTORY}),
        writable=frozenset(),
    ),
    "viewer": DomainAccess(visible=frozenset({DataDomain.SALES}), writable=frozenset()),
    "salesperson": DomainAccess(
        visible=frozenset({DataDomain.SALES}), writable=frozenset({DataDomain.SALES})
    ),
    "purchaser": DomainAccess(
        visible=frozenset({DataDomain.PURCHASE}),
        writable=frozenset({DataDomain.PURCHASE}),
    ),
    "warehouse": DomainAccess(
        visible=frozenset({DataDomain.INVENTORY}),
        writable=frozenset({DataDomain.INVENTORY}),
    ),
    "finance_staff": DomainAccess(
        visible=frozenset({DataDomain.FINANCE, DataDomain.SALES, DataDomain.PURCHASE}),
        writable=frozenset({DataDomain.FINANCE}),
    ),
}

# 行级过滤：data_scope.org_ids → 单据组织字段（单组织等值；多组织暂不支持）
ROW_SCOPE_FIELD_MAP: dict[str, str] = {
    "SAL_Order": "FSaleOrgId.FNumber",
}

# 行级过滤：data_scope.owner_only → 单据负责人字段
ROW_OWNER_FIELD_MAP: dict[str, str] = {
    "SAL_Order": "FSaleEmpId",  # INFERRED: 待真环境复核字段名
}

# 列级权限：按数据域禁止输出的字段（T10/M7 在结果出口剔除）
COLUMN_DENY_MAP: dict[DataDomain, frozenset[str]] = {
    DataDomain.FINANCE: frozenset({"FBankAccount"}),
    DataDomain.HR: frozenset({"FSalary", "FIdCard"}),
}


@dataclass(frozen=True)
class WriteRule:
    """写操作权限规则。

    ``max_amount`` 为 None 时匹配所有金额；否则仅当金额超过阈值才命中。
    ``deny`` 优先于 ``escalate``（拒绝 > 升级 > 放行）。
    """

    rule_id: str
    label: str
    roles: tuple[str, ...] = ()  # 空 = 所有角色
    form_ids: tuple[str, ...] = ()  # 空 = 所有表单
    actions: tuple[str, ...] = ()  # 空 = 所有动作
    max_amount: float | None = None
    decision: Literal["deny", "escalate"] = "deny"
    escalate_to: str = ""


WRITE_RULES: tuple[WriteRule, ...] = (
    WriteRule(
        rule_id="SAL_AMOUNT_LIMIT",
        label="销售订单金额上限",
        roles=("salesperson",),
        form_ids=("SAL_Order",),
        actions=("save", "submit"),
        max_amount=100_000,
        decision="escalate",
        escalate_to="sales_manager",
    ),
    WriteRule(
        rule_id="NO_DELETE_PRODUCTION",
        label="生产单据禁止删除",
        form_ids=("PRD_Order", "PRD_Report"),
        actions=("delete",),
        decision="deny",
    ),
)
