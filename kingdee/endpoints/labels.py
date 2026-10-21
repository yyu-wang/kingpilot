"""写工具与参数的中文展示名（审批卡片、确认摘要共用的单一真相源）。

工具中文名由写工具绑定推导：``动作标签 + 端点 label``，新增写工具自动生效，
不需要再维护一份工具名清单；参数标签集中声明，未知参数回退原始键名
（回退而非隐藏，避免误吞新工具参数）。
"""

from __future__ import annotations

from kingdee.endpoints.pur_order_write import PURCHASE_ORDER_WRITE_TOOLS
from kingdee.endpoints.sal_order_write import SALES_ORDER_WRITE_TOOLS
from kingdee.endpoints.stk_inbound_write import PURCHASE_INBOUND_WRITE_TOOLS

#: 金蝶 DynamicFormService 动作 → 中文动词
ACTION_LABELS: dict[str, str] = {
    "save": "创建",
    "submit": "提交",
    "audit": "审核",
    "unaudit": "反审核",
    "delete": "删除",
}

#: 写工具参数名 → 中文标签（面向审批人的业务说法，不复用长篇 description）
PARAM_LABELS: dict[str, str] = {
    "bill_no": "单据编号",
    "source_bill_no": "源采购订单号",
    "customer_number": "客户",
    "supplier_number": "供应商",
    "material_number": "物料",
    "warehouse_name": "仓库",
    "quantity": "数量",
    "tax_price": "含税单价",
    "order_date": "单据日期",
    "inbound_date": "入库日期",
    "note": "备注",
}

#: 实体类型 → 中文名（Guard 3 实体预检提示共用）
ENTITY_LABELS: dict[str, str] = {
    "customer": "客户",
    "supplier": "供应商",
    "material": "物料",
    "warehouse": "仓库",
    "order": "单据",
}

_WRITE_BINDINGS = (
    *SALES_ORDER_WRITE_TOOLS,
    *PURCHASE_ORDER_WRITE_TOOLS,
    *PURCHASE_INBOUND_WRITE_TOOLS,
)

#: 工具名 → 中文展示名（如 kingdee_create_sales_order → 创建销售订单）
TOOL_LABELS: dict[str, str] = {
    binding.tool_name: (
        f"{ACTION_LABELS.get(binding.action, binding.action)}{binding.endpoint.label}"
    )
    for binding in _WRITE_BINDINGS
}


def tool_label(tool_name: str) -> str:
    """工具名 → 中文展示名；未知工具回退原值。"""
    return TOOL_LABELS.get(tool_name, tool_name)


def param_label(name: str) -> str:
    """参数名 → 中文标签；未知参数回退原值。"""
    return PARAM_LABELS.get(name, name)


def entity_label(entity_type: str) -> str:
    """实体类型 → 中文名；未知类型回退原值。"""
    return ENTITY_LABELS.get(entity_type, entity_type)
