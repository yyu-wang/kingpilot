"""端点描述导出（M1-T05 / M2-T03）。

按 formId 组织端点描述，字段白名单在此维护（M1 模块规范 5）。
新增 formId 时在同一模式下落一个新模块并在此导出；带 ``_TOOL`` 后缀的
是查询工具绑定三件套（agent/tools.py 组装 ToolDefinition 用）。
"""

from kingdee.endpoints.base import (
    EndpointDescriptor,
    EndpointParams,
    FieldDescriptor,
    FieldType,
    QueryToolBinding,
)
from kingdee.endpoints.bd_customer import CUSTOMER, CUSTOMER_TOOL
from kingdee.endpoints.bd_supplier import SUPPLIER, SUPPLIER_TOOL
from kingdee.endpoints.pur_order import PURCHASE_ORDER, PURCHASE_ORDER_TOOL
from kingdee.endpoints.sal_order import SAL_ORDER, SAL_ORDER_TOOL
from kingdee.endpoints.stk_inventory import INVENTORY, INVENTORY_TOOL

__all__ = [
    "CUSTOMER",
    "CUSTOMER_TOOL",
    "INVENTORY",
    "INVENTORY_TOOL",
    "PURCHASE_ORDER",
    "PURCHASE_ORDER_TOOL",
    "SAL_ORDER",
    "SAL_ORDER_TOOL",
    "SUPPLIER",
    "SUPPLIER_TOOL",
    "EndpointDescriptor",
    "EndpointParams",
    "FieldDescriptor",
    "FieldType",
    "QueryToolBinding",
]
