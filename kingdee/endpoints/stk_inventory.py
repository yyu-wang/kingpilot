"""即时库存（STK_Inventory）端点描述与查询工具绑定（M2-T03）。

**全部字段 INFERRED**：STK_Inventory 的字段拼写歧义最大（数量字段在
FQty/FBaseQty 间存在版本差异），取 FBaseQty（基本单位数量）为保守值，
T01 真环境联调时优先复核本端点。
"""

from typing import cast

from pydantic import Field

from erp import FilterExpression, eq
from erp.tool_names import QUERY_INVENTORY
from kingdee.endpoints.base import (
    EndpointDescriptor,
    EndpointParams,
    FieldDescriptor,
    QueryToolBinding,
)
from models.base import KingPilotBaseModel

# INFERRED: 全部字段拼写待真环境复核（数量字段版本歧义最大）
_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(
        name="FMaterialId.FNumber",
        data_type="related",
        filterable=True,
        description="物料编码（过滤用）",
    ),
    FieldDescriptor(
        name="FMaterialId.FName",
        data_type="related",
        filterable=False,
        description="物料名称，显示用",
    ),
    FieldDescriptor(
        name="FStockId.FName", data_type="related", filterable=False, description="仓库名称，显示用"
    ),
    FieldDescriptor(
        name="FBaseQty", data_type="number", filterable=False, description="基本单位库存量"
    ),
)  # INFERRED: 待真环境复核字段名

INVENTORY = EndpointDescriptor(
    form_id="STK_Inventory",
    label="即时库存",
    fields=_FIELDS,
    default_field_keys=("FMaterialId.FNumber", "FMaterialId.FName", "FStockId.FName", "FBaseQty"),
    default_sort="FMaterialId.FNumber",
)


class InventoryParams(EndpointParams):
    """查询库存的结构化参数：物料编码精确匹配。"""

    material_number: str = Field(default="", description="物料编码精确匹配（如 MAT001）")


def build_inventory_filter(params: KingPilotBaseModel) -> FilterExpression:
    """参数 → FilterExpression；条件字段均在 INVENTORY.filterable_fields 内。"""
    p = cast(InventoryParams, params)
    if p.material_number:
        return FilterExpression(conditions=(eq("FMaterialId.FNumber", p.material_number),))
    return FilterExpression()


INVENTORY_TOOL = QueryToolBinding(
    tool_name=QUERY_INVENTORY,
    tool_description=(
        "按物料编码查询即时库存（金蝶 STK_Inventory）。"
        "何时用：用户询问某物料的库存量/分布在哪些仓库时。"
        "返回物料编码/名称/仓库/库存量。"
    ),
    endpoint=INVENTORY,
    params_model=InventoryParams,
    build_filter=build_inventory_filter,
)
