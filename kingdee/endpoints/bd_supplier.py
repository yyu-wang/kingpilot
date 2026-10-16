"""供应商基础资料（BD_Supplier）端点描述与查询工具绑定（M2-T03）。

**全部字段 INFERRED**：结构与客户端点同构（编码/名称/简称），待真环境复核。
"""

from typing import cast

from pydantic import Field

from kingdee.endpoints.base import (
    EndpointDescriptor,
    EndpointParams,
    FieldDescriptor,
    QueryToolBinding,
    quote_like,
)
from models.base import KingPilotBaseModel

# INFERRED: 全部字段拼写待真环境复核
_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(name="FNumber", data_type="string", filterable=True, description="供应商编码"),
    FieldDescriptor(
        name="FName", data_type="string", filterable=True, description="供应商名称（支持模糊查询）"
    ),
    FieldDescriptor(
        name="FShortName", data_type="string", filterable=False, description="供应商简称"
    ),
)  # INFERRED: 待真环境复核字段名

SUPPLIER = EndpointDescriptor(
    form_id="BD_Supplier",
    label="供应商",
    fields=_FIELDS,
    default_field_keys=("FNumber", "FName", "FShortName"),
    default_sort="FNumber",
)


class SupplierParams(EndpointParams):
    """查询供应商的结构化参数：名称模糊匹配。"""

    name: str = Field(default="", description="供应商名称（模糊匹配，可只给部分字）")


def build_supplier_filter(params: KingPilotBaseModel) -> str:
    """参数 → FilterString；条件字段均在 SUPPLIER.filterable_fields 内。"""
    p = cast(SupplierParams, params)
    if p.name:
        return f"FName LIKE {quote_like(p.name)}"
    return ""


SUPPLIER_TOOL = QueryToolBinding(
    tool_name="kingdee_query_supplier",
    tool_description=(
        "按名称模糊查询供应商基础资料（金蝶 BD_Supplier）。"
        "何时用：需要把供应商名称换成供应商编码时（采购订单查询只支持编码过滤）。"
        "返回供应商编码/名称/简称。"
    ),
    endpoint=SUPPLIER,
    params_model=SupplierParams,
    build_filter=build_supplier_filter,
)
