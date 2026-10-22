"""采购订单（PUR_PurchaseOrder）端点描述与查询工具绑定（M2-T03）。

**全部字段 INFERRED**：PUR_PurchaseOrder 是金蝶标准 FormId，字段拼写与
SAL_Order 同风格但未经真环境验证（T01 凭证到位后复核）。单据状态值域
与销售订单共用（Z/A/B/C/D，金蝶单据状态通用枚举）。
"""

from typing import cast

from pydantic import Field, field_validator

from erp import FilterCondition, FilterExpression, eq, ge
from erp.tool_names import QUERY_PURCHASE_ORDER
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
    FieldDescriptor(name="FBillNo", data_type="string", filterable=True, description="单据编号"),
    FieldDescriptor(name="FDate", data_type="date", filterable=True, description="单据日期"),
    FieldDescriptor(
        name="FDocumentStatus",
        data_type="enum",
        filterable=True,
        description="单据状态（Z/A/B/C/D）",
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
        name="FAmount", data_type="number", filterable=True, description="金额合计（表头）"
    ),
    FieldDescriptor(
        name="FMaterialId.FNumber",
        data_type="related",
        filterable=True,
        description="物料编码（沙箱简化为单行）",
    ),
    FieldDescriptor(
        name="FMaterialId.FName",
        data_type="related",
        filterable=False,
        description="物料名称，显示用",
    ),
    FieldDescriptor(name="FQty", data_type="number", filterable=True, description="采购数量"),
    FieldDescriptor(name="FTaxPrice", data_type="number", filterable=True, description="含税单价"),
)  # INFERRED: 待真环境复核字段名

PURCHASE_ORDER = EndpointDescriptor(
    form_id="PUR_PurchaseOrder",
    label="采购订单",
    fields=_FIELDS,
    default_field_keys=("FBillNo", "FDate", "FSupplierID.FName", "FDocumentStatus", "FAmount"),
    default_sort="FBillNo",
)


class PurchaseOrderParams(EndpointParams):
    """查询采购订单的结构化参数；供应商仅支持编码过滤（换码交给供应商工具）。"""

    supplier_number: str = Field(
        default="",
        description="供应商编码精确匹配；按名称查请先用供应商查询工具换编码",
    )
    document_status: str = Field(
        default="",
        description="单据状态：Z 暂存 / A 创建 / B 审核中 / C 已审核 / D 重新审核",
    )
    min_amount: float | None = Field(default=None, gt=0, description="金额下限（表头合计）")
    bill_no: str = Field(default="", description="单据编号精确匹配（如 PO000001）")
    order_date: str = Field(
        default="",
        description="单据日期精确匹配，格式 YYYY-MM-DD（如 2026-01-03）；范围查询暂不支持",
    )
    material_number: str = Field(default="", description="物料编码精确匹配（如 MAT001）")
    min_quantity: float | None = Field(default=None, gt=0, description="采购数量下限")

    _check_status = field_validator("document_status")(validate_document_status)


def build_purchase_order_filter(params: KingPilotBaseModel) -> FilterExpression:
    """参数 → FilterExpression；条件字段均在 PURCHASE_ORDER.filterable_fields 内。"""
    p = cast(PurchaseOrderParams, params)
    conditions: list[FilterCondition] = []
    if p.supplier_number:
        conditions.append(eq("FSupplierID.FNumber", p.supplier_number))
    if p.document_status:
        conditions.append(eq("FDocumentStatus", p.document_status))
    if p.min_amount is not None:
        conditions.append(ge("FAmount", p.min_amount))
    if p.bill_no:
        conditions.append(eq("FBillNo", p.bill_no))
    if p.order_date:
        conditions.append(eq("FDate", p.order_date))
    if p.material_number:
        conditions.append(eq("FMaterialId.FNumber", p.material_number))
    if p.min_quantity is not None:
        conditions.append(ge("FQty", p.min_quantity))
    return FilterExpression(conditions=tuple(conditions))


PURCHASE_ORDER_TOOL = QueryToolBinding(
    tool_name=QUERY_PURCHASE_ORDER,
    tool_description=(
        "按条件查询采购订单列表（金蝶 PUR_PurchaseOrder）。"
        "何时用：用户询问采购订单、到货状态、某供应商的订单时。"
        "所有参数可选；供应商仅支持按编码过滤，只有名称时请先用"
        " erp_query_supplier 查编码。返回单据编号/日期/供应商/状态/金额。"
    ),
    endpoint=PURCHASE_ORDER,
    params_model=PurchaseOrderParams,
    build_filter=build_purchase_order_filter,
)
