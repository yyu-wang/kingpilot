"""客户基础资料（BD_Customer）端点描述与查询工具绑定（M2-T03）。

**全部字段 INFERRED**：BD_Customer 是金蝶标准 FormId，但字段拼写未经真环境
验证（T01 凭证到位后用全字段查询比对复核）。字段集取最小保守值，修正只改本文件。

工具定位：客户名称 → 客户编码的**换码器**。订单类工具只支持按编码过滤
（白名单语义「可查 ≠ 可过滤」），名称模糊查询集中在本工具完成。
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
    FieldDescriptor(name="FNumber", data_type="string", filterable=True, description="客户编码"),
    FieldDescriptor(
        name="FID",
        data_type="number",
        filterable=False,
        description="内部主键（M4 写操作实体引用预检用，不可过滤）",
    ),
    FieldDescriptor(
        name="FName", data_type="string", filterable=True, description="客户名称（支持模糊查询）"
    ),
    FieldDescriptor(
        name="FShortName", data_type="string", filterable=False, description="客户简称"
    ),
)  # INFERRED: 待真环境复核字段名

CUSTOMER = EndpointDescriptor(
    form_id="BD_Customer",
    label="客户",
    fields=_FIELDS,
    default_field_keys=("FNumber", "FName", "FShortName"),
    default_sort="FNumber",
)


class CustomerParams(EndpointParams):
    """查询客户的结构化参数：名称模糊匹配（编码精确查询无客户端场景，不设）。"""

    name: str = Field(default="", description="客户名称（模糊匹配，可只给部分字）")


def build_customer_filter(params: KingPilotBaseModel) -> str:
    """参数 → FilterString；条件字段均在 CUSTOMER.filterable_fields 内。"""
    p = cast(CustomerParams, params)
    if p.name:
        return f"FName LIKE {quote_like(p.name)}"
    return ""


CUSTOMER_TOOL = QueryToolBinding(
    tool_name="kingdee_query_customer",
    tool_description=(
        "按名称模糊查询客户基础资料（金蝶 BD_Customer）。"
        "何时用：需要把客户名称换成客户编码时（订单类查询只支持编码过滤）。"
        "返回客户编码/名称/简称。"
    ),
    endpoint=CUSTOMER,
    params_model=CustomerParams,
    build_filter=build_customer_filter,
)
