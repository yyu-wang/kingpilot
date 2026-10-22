"""工具名常量（LLM 可见契约的单一真相源）。

Why 集中而非散落在各端点：
- 工具名同时被端点绑定、规则引擎（降级补全）、Saga 补偿映射、评测集引用；
  散落时改名只能靠全局替换，而历史上有 3 处是用 f-string 拼出来的
  （``kingdee_{action}_sales_order``），全局替换抓不到。
- 常量引用错了会被 ruff/mypy 当场发现；拼字符串错了只会在运行时表现为
  「未知工具」，更晚、更难查。

Why ``erp_`` 前缀而非 ``kingdee_``：
工具名是 LLM 可见面。产品定位是「通用 ERP Agent + 金蝶作为参考适配器」，
前缀带厂商名会让新增用友/SAP 时出现「用金蝶的工具有调用友接口」的穿帮。

动作词说明：工具名里的动作段是 ``create``（不是金蝶动作 ``save``），
与 ``agent/rule_engine.py`` 的确定性补全保持一致。
"""

from __future__ import annotations

__all__ = [
    "AUDIT_PURCHASE_INBOUND",
    "AUDIT_PURCHASE_ORDER",
    "AUDIT_SALES_ORDER",
    "CREATE_PURCHASE_INBOUND",
    "CREATE_PURCHASE_ORDER",
    "CREATE_SALES_ORDER",
    "DELETE_PURCHASE_INBOUND",
    "DELETE_PURCHASE_ORDER",
    "DELETE_SALES_ORDER",
    "OBJ_CUSTOMER",
    "OBJ_INVENTORY",
    "OBJ_PURCHASE_INBOUND",
    "OBJ_PURCHASE_ORDER",
    "OBJ_SALES_ORDER",
    "OBJ_SUPPLIER",
    "QUERY_CUSTOMER",
    "QUERY_INVENTORY",
    "QUERY_PURCHASE_INBOUND",
    "QUERY_PURCHASE_ORDER",
    "QUERY_SALES_ORDER",
    "QUERY_SUPPLIER",
    "QUERY_TOOL_NAMES",
    "SUBMIT_PURCHASE_INBOUND",
    "SUBMIT_PURCHASE_ORDER",
    "SUBMIT_SALES_ORDER",
    "UNAUDIT_PURCHASE_INBOUND",
    "UNAUDIT_PURCHASE_ORDER",
    "UNAUDIT_SALES_ORDER",
    "WRITE_TOOL_NAMES",
    "write_tool_name",
]

# ── 业务对象短名（与 agent/rule_engine.py 的表单关键词一致）──
# 带 OBJ_ 前缀避免与各端点模块里的 EndpointDescriptor 常量同名
# （如 kingdee.endpoints.pur_order.PURCHASE_ORDER）
OBJ_SALES_ORDER = "sales_order"
OBJ_PURCHASE_ORDER = "purchase_order"
OBJ_PURCHASE_INBOUND = "purchase_inbound"
OBJ_CUSTOMER = "customer"
OBJ_SUPPLIER = "supplier"
OBJ_INVENTORY = "inventory"

# ── 查询工具（6）──
QUERY_SALES_ORDER = "erp_query_sales_order"
QUERY_PURCHASE_ORDER = "erp_query_purchase_order"
QUERY_PURCHASE_INBOUND = "erp_query_purchase_inbound"
QUERY_CUSTOMER = "erp_query_customer"
QUERY_SUPPLIER = "erp_query_supplier"
QUERY_INVENTORY = "erp_query_inventory"

# ── 写 / 动作工具（15）──
CREATE_SALES_ORDER = "erp_create_sales_order"
SUBMIT_SALES_ORDER = "erp_submit_sales_order"
AUDIT_SALES_ORDER = "erp_audit_sales_order"
UNAUDIT_SALES_ORDER = "erp_unaudit_sales_order"
DELETE_SALES_ORDER = "erp_delete_sales_order"

CREATE_PURCHASE_ORDER = "erp_create_purchase_order"
SUBMIT_PURCHASE_ORDER = "erp_submit_purchase_order"
AUDIT_PURCHASE_ORDER = "erp_audit_purchase_order"
UNAUDIT_PURCHASE_ORDER = "erp_unaudit_purchase_order"
DELETE_PURCHASE_ORDER = "erp_delete_purchase_order"

CREATE_PURCHASE_INBOUND = "erp_create_purchase_inbound"
SUBMIT_PURCHASE_INBOUND = "erp_submit_purchase_inbound"
AUDIT_PURCHASE_INBOUND = "erp_audit_purchase_inbound"
UNAUDIT_PURCHASE_INBOUND = "erp_unaudit_purchase_inbound"
DELETE_PURCHASE_INBOUND = "erp_delete_purchase_inbound"

#: 对象短名 → 查询工具名
QUERY_TOOL_NAMES: dict[str, str] = {
    OBJ_SALES_ORDER: QUERY_SALES_ORDER,
    OBJ_PURCHASE_ORDER: QUERY_PURCHASE_ORDER,
    OBJ_PURCHASE_INBOUND: QUERY_PURCHASE_INBOUND,
    OBJ_CUSTOMER: QUERY_CUSTOMER,
    OBJ_SUPPLIER: QUERY_SUPPLIER,
    OBJ_INVENTORY: QUERY_INVENTORY,
}

#: (对象短名, 动作段) → 写工具名；动作段取 create/submit/audit/unaudit/delete
WRITE_TOOL_NAMES: dict[tuple[str, str], str] = {
    (OBJ_SALES_ORDER, "create"): CREATE_SALES_ORDER,
    (OBJ_SALES_ORDER, "submit"): SUBMIT_SALES_ORDER,
    (OBJ_SALES_ORDER, "audit"): AUDIT_SALES_ORDER,
    (OBJ_SALES_ORDER, "unaudit"): UNAUDIT_SALES_ORDER,
    (OBJ_SALES_ORDER, "delete"): DELETE_SALES_ORDER,
    (OBJ_PURCHASE_ORDER, "create"): CREATE_PURCHASE_ORDER,
    (OBJ_PURCHASE_ORDER, "submit"): SUBMIT_PURCHASE_ORDER,
    (OBJ_PURCHASE_ORDER, "audit"): AUDIT_PURCHASE_ORDER,
    (OBJ_PURCHASE_ORDER, "unaudit"): UNAUDIT_PURCHASE_ORDER,
    (OBJ_PURCHASE_ORDER, "delete"): DELETE_PURCHASE_ORDER,
    (OBJ_PURCHASE_INBOUND, "create"): CREATE_PURCHASE_INBOUND,
    (OBJ_PURCHASE_INBOUND, "submit"): SUBMIT_PURCHASE_INBOUND,
    (OBJ_PURCHASE_INBOUND, "audit"): AUDIT_PURCHASE_INBOUND,
    (OBJ_PURCHASE_INBOUND, "unaudit"): UNAUDIT_PURCHASE_INBOUND,
    (OBJ_PURCHASE_INBOUND, "delete"): DELETE_PURCHASE_INBOUND,
}


def write_tool_name(object_name: str, action: str) -> str | None:
    """按（对象, 动作段）查写工具名；未登记的组合返回 None（调用方不补全）。"""
    return WRITE_TOOL_NAMES.get((object_name, action))
