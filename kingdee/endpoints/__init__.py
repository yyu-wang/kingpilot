"""端点描述导出（M1-T05）。

按 formId 组织端点描述，字段白名单在此维护（M1 模块规范 5）。
新增 formId 时在同一模式下落一个新模块并在此导出。
"""

from kingdee.endpoints.base import EndpointDescriptor, FieldDescriptor, FieldType
from kingdee.endpoints.sal_order import SAL_ORDER

__all__ = ["SAL_ORDER", "EndpointDescriptor", "FieldDescriptor", "FieldType"]
