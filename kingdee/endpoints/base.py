"""端点描述符基类（M1-T05）——过渡期 re-export 薄壳。

T03 起，``FieldDescriptor`` / ``EndpointDescriptor`` / ``EndpointParams`` /
``QueryToolBinding`` / ``WriteToolBinding`` / ``EntityReference`` /
``validate_document_status`` 已上收至 ``erp/descriptors.py`` 与
``erp/bindings.py``（厂商中立契约层）。本模块仅保留 re-export 以维持
现有导入路径（``from kingdee.endpoints.base import ...``）的源码兼容，
后续里程碑再让消费方直接 import erp.* 并删除本壳。

Why 不立即删除：全仓 195 处 ``from kingdee.`` 导入的迁移是 M5+ 的工作，
本壳把「搬迁」与「改导入」解耦，单次重构半径可控。

``quote_eq`` / ``quote_like`` / ``fmt_number`` 已迁至 ``kingdee/filter_string.py``
（金蝶 FilterString 渲染器的内部实现），本模块 re-export 供旧调用点
过渡期使用；新代码应直接 ``from kingdee.filter_string import ...``。
"""

from erp.bindings import (
    EndpointParams,
    EntityReference,
    QueryToolBinding,
    WriteRemoteRunner,
    WriteToolBinding,
    validate_document_status,
)
from erp.descriptors import EndpointDescriptor, FieldDescriptor, FieldType
from kingdee.filter_string import fmt_number, quote_eq, quote_like

__all__ = [
    "EndpointDescriptor",
    "EndpointParams",
    "EntityReference",
    "FieldDescriptor",
    "FieldType",
    "QueryToolBinding",
    "WriteRemoteRunner",
    "WriteToolBinding",
    "fmt_number",
    "quote_eq",
    "quote_like",
    "validate_document_status",
]
