"""采购入库单（STK_InStock）端点描述与查询工具绑定（采购入库流程测试数据）。

业务定位：采购订单到货后由仓库执行的**入库单据**，与采购订单是两个对象：

- 采购订单（PUR_PurchaseOrder）：向供应商下的单
- 采购入库单（STK_InStock）：实际到货入库的记录，可通过源单号回链采购订单

**全部字段 INFERRED**：字段拼写待真环境联调复核；沙箱按本描述造数。
"""

from typing import cast

from pydantic import Field, field_validator

from erp import FilterCondition, FilterExpression, eq, ge
from erp.tool_names import QUERY_PURCHASE_INBOUND
from kingdee.endpoints.base import (
    EndpointDescriptor,
    EndpointParams,
    FieldDescriptor,
    QueryToolBinding,
    validate_document_status,
)
from models.base import KingPilotBaseModel

# INFERRED: 全部字段拼写待真环境复核
_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(name="FBillNo", data_type="string", filterable=True, description="入库单编号"),
    FieldDescriptor(name="FDate", data_type="date", filterable=True, description="入库日期"),
    FieldDescriptor(
        name="FDocumentStatus",
        data_type="enum",
        filterable=True,
        description="单据状态：Z 暂存 / A 创建 / B 审核中 / C 已审核（已入库）/ D 重新审核",
    ),
    FieldDescriptor(
        name="FSupplierID.FNumber",
        data_type="related",
        filterable=True,
        description="供应商编码（过滤用）",
    ),
    FieldDescriptor(
        name="FSupplierID.FName",
        data_type="related",
        filterable=False,
        description="供应商名称，显示用",
    ),
    FieldDescriptor(
        name="FStockId.FName",
        data_type="related",
        filterable=True,
        description="仓库名称（过滤用，如 成品仓/原料仓）",
    ),
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
    FieldDescriptor(name="FRealQty", data_type="number", filterable=True, description="实收数量"),
    FieldDescriptor(
        name="FSrcBillNo",
        data_type="string",
        filterable=True,
        description="源采购订单号（回链采购订单，过滤用）",
    ),
    FieldDescriptor(name="FAmount", data_type="number", filterable=True, description="入库金额"),
)  # INFERRED: 待真环境复核字段名

PURCHASE_INBOUND = EndpointDescriptor(
    form_id="STK_InStock",
    label="采购入库单",
    fields=_FIELDS,
    default_field_keys=(
        "FBillNo",
        "FDate",
        "FSupplierID.FName",
        "FStockId.FName",
        "FMaterialId.FNumber",
        "FRealQty",
        "FDocumentStatus",
    ),
    default_sort="FBillNo",
)


class PurchaseInboundParams(EndpointParams):
    """查询采购入库单的结构化参数。"""

    bill_no: str = Field(default="", description="入库单编号精确匹配（如 CGRK000001）")
    supplier_number: str = Field(
        default="",
        description="供应商编码精确匹配；按名称查请先用供应商查询工具换编码",
    )
    material_number: str = Field(default="", description="物料编码精确匹配（如 MAT001）")
    warehouse_name: str = Field(default="", description="仓库名称精确匹配（如 成品仓）")
    document_status: str = Field(
        default="",
        description="单据状态：Z 暂存 / A 创建 / B 审核中 / C 已审核（已入库）/ D 重新审核",
    )
    order_date: str = Field(
        default="",
        description="入库日期精确匹配，格式 YYYY-MM-DD（如 2026-01-06）",
    )
    source_bill_no: str = Field(
        default="",
        description="源采购订单号精确匹配（如 PO000001），用于查询某采购订单的入库情况",
    )
    min_quantity: float | None = Field(default=None, gt=0, description="实收数量下限")

    _check_status = field_validator("document_status")(validate_document_status)


def build_purchase_inbound_filter(params: KingPilotBaseModel) -> FilterExpression:
    """参数 → FilterExpression；条件字段均在 PURCHASE_INBOUND.filterable_fields 内。"""
    p = cast(PurchaseInboundParams, params)
    conditions: list[FilterCondition] = []
    if p.bill_no:
        conditions.append(eq("FBillNo", p.bill_no))
    if p.supplier_number:
        conditions.append(eq("FSupplierID.FNumber", p.supplier_number))
    if p.material_number:
        conditions.append(eq("FMaterialId.FNumber", p.material_number))
    if p.warehouse_name:
        conditions.append(eq("FStockId.FName", p.warehouse_name))
    if p.document_status:
        conditions.append(eq("FDocumentStatus", p.document_status))
    if p.order_date:
        conditions.append(eq("FDate", p.order_date))
    if p.source_bill_no:
        conditions.append(eq("FSrcBillNo", p.source_bill_no))
    if p.min_quantity is not None:
        conditions.append(ge("FRealQty", p.min_quantity))
    return FilterExpression(conditions=tuple(conditions))


PURCHASE_INBOUND_TOOL = QueryToolBinding(
    tool_name=QUERY_PURCHASE_INBOUND,
    tool_description=(
        "查询采购入库单（金蝶 STK_InStock），即采购订单到货后仓库实际收货入库的记录。"
        "何时用：用户询问采购入库单、到货入库记录、某采购订单收了多少货、"
        "某供应商/物料/仓库的入库情况时。"
        "采购订单本身请用 erp_query_purchase_order；"
        "可按入库单号、供应商编码、物料编码、仓库、状态、入库日期、源采购订单号、"
        "实收数量下限过滤。返回入库单号/日期/供应商/仓库/物料/实收数量/状态。"
        "状态含义：Z 暂存、A 创建、B 审核中、C 已审核（已入库）、D 重新审核；"
        "D 不是作废/关闭，不要误读。"
    ),
    endpoint=PURCHASE_INBOUND,
    params_model=PurchaseInboundParams,
    build_filter=build_purchase_inbound_filter,
)
