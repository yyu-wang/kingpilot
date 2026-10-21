"""销售订单（SAL_Order）端点描述（M1-T05）。

字段清单来源：金蝶云星空官方文档与社区高频用法。**无真实环境的限制**——
部分字段拼写未经真环境验证，已用 ``# INFERRED:`` 标注，待 T06b 真环境联调时
用「全字段查询返回列」比对复核。字段名集中在本文件一处，修正只需改一行。

默认查询集与默认排序取最保守值：默认排序用 ``FBillNo`` 而非 ``FDate DESC``，
避免与 M4 Guard 2 的排序字段校验职责重叠。
"""

from typing import cast

from pydantic import Field, field_validator

from kingdee.endpoints.base import (
    EndpointDescriptor,
    EndpointParams,
    FieldDescriptor,
    QueryToolBinding,
    fmt_number,
    quote_eq,
    validate_document_status,
)
from models.base import KingPilotBaseModel

# 字段顺序 = 默认查询结果的列顺序（便于人工核对）
_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(
        name="FBillNo",
        data_type="string",
        filterable=True,
        description="单据编号（业务主键，人工可读）",
    ),
    FieldDescriptor(
        name="FID",
        data_type="number",
        filterable=False,
        description="单据内部主键，仅供跳转，不作为过滤条件",
    ),
    FieldDescriptor(name="FDate", data_type="date", filterable=True, description="单据日期"),
    FieldDescriptor(
        name="FDocumentStatus",
        data_type="enum",
        filterable=True,
        description="单据状态，取值见 models.kingdee.DocumentStatus（Z/A/B/C/D）",
    ),
    FieldDescriptor(
        name="FCloseStatus",
        data_type="enum",
        filterable=True,
        description="关闭状态（正常/已关闭）",
    ),
    FieldDescriptor(
        name="FBillTypeID.FNumber",
        data_type="related",
        filterable=True,
        description="单据类型编码（如标准销售订单）",
    ),
    FieldDescriptor(
        name="FCustomerID.FName",
        data_type="related",
        filterable=False,
        description="客户名称，显示用；过滤请改用 FCustomerID.FNumber",
    ),
    FieldDescriptor(
        name="FCustomerID.FNumber",
        data_type="related",
        filterable=True,
        description="客户编码（过滤用）",
    ),
    FieldDescriptor(
        name="FSaleOrgId.FNumber", data_type="related", filterable=True, description="销售组织编码"
    ),
    FieldDescriptor(
        name="FSalerId.FName",
        data_type="related",
        filterable=False,
        description="销售员姓名，显示用",
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FCurrencyId.FNumber", data_type="related", filterable=True, description="币别编码"
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FBillAllAmount", data_type="number", filterable=True, description="金额合计（表头）"
    ),  # INFERRED: 待真环境复核拼写
    FieldDescriptor(
        name="FCreateDate", data_type="date", filterable=True, description="创建日期（审计字段）"
    ),
    FieldDescriptor(
        name="FEntryID",
        data_type="number",
        filterable=False,
        description="明细行 ID（明细字段，混查会使一张单占多行）",
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FMaterialId.FNumber",
        data_type="related",
        filterable=True,
        description="物料编码（明细行）",
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FMaterialId.FName",
        data_type="related",
        filterable=False,
        description="物料名称（明细行，显示用）",
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FQty", data_type="number", filterable=True, description="数量（明细行）"
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FTaxPrice", data_type="number", filterable=True, description="含税单价（明细行）"
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FDeliveryDate", data_type="date", filterable=True, description="交货日期（明细行）"
    ),  # INFERRED: 待真环境复核字段名
    FieldDescriptor(
        name="FNote", data_type="string", filterable=False, description="备注"
    ),  # INFERRED: 待真环境复核字段名
)

SAL_ORDER = EndpointDescriptor(
    form_id="SAL_Order",
    label="销售订单",
    fields=_FIELDS,
    default_field_keys=(
        "FBillNo",
        "FDate",
        "FCustomerID.FName",
        "FDocumentStatus",
        "FBillAllAmount",
    ),
    default_sort="FBillNo",
)


# ── 查询工具绑定（M2-T03）：结构化参数 + FilterString 组装 ──
# 注意：客户过滤只提供 FCustomerID.FNumber（白名单语义「可查 ≠ 可过滤」）——
# LLM 手上通常是客户名称，名称→编码的转换交给 kingdee_query_customer 工具
# 多步完成，这是 Agent 编排职责，不在工具内偷偷用名称过滤。


class SalesOrderParams(EndpointParams):
    """查询销售订单的结构化参数（LLM 只能选值，不接触 FilterString 原文）。"""

    customer_number: str = Field(
        default="",
        description="客户编码精确匹配（如 CUS001）；按名称查请先用客户查询工具换编码",
    )
    document_status: str = Field(
        default="",
        description="单据状态：Z 暂存 / A 创建 / B 审核中 / C 已审核 / D 重新审核",
    )
    min_amount: float | None = Field(default=None, gt=0, description="金额下限（表头合计）")
    bill_no: str = Field(default="", description="单据编号精确匹配（如 SO000008）")
    order_date: str = Field(
        default="",
        description="单据日期精确匹配，格式 YYYY-MM-DD（如 2026-03-05）；范围查询暂不支持",
    )
    include_entries: bool = Field(
        default=False,
        description=(
            "是否返回物料明细行。用户问「用了哪些物料 / 明细 / 数量 / 单价」时置 true；"
            "此时结果按明细行展开（一张单可能返回多行）"
        ),
    )

    _check_status = field_validator("document_status")(validate_document_status)


# 明细行字段：include_entries=true 时追加到默认表头字段之后
_ENTRY_FIELD_KEYS: tuple[str, ...] = (
    "FEntryID",
    "FMaterialId.FNumber",
    "FMaterialId.FName",
    "FQty",
    "FTaxPrice",
    "FDeliveryDate",
)


def build_sales_order_field_keys(params: KingPilotBaseModel) -> str | None:
    """参数 → FieldKeys；不展开明细时返回 None（走端点默认表头字段）。"""
    p = cast(SalesOrderParams, params)
    if not p.include_entries:
        return None
    return ",".join((*SAL_ORDER.default_field_keys, *_ENTRY_FIELD_KEYS))


def build_sales_order_filter(params: KingPilotBaseModel) -> str:
    """参数 → FilterString；全部条件字段均在 SAL_ORDER.filterable_fields 内。"""
    p = cast(SalesOrderParams, params)
    conditions: list[str] = []
    if p.customer_number:
        conditions.append(f"FCustomerID.FNumber = {quote_eq(p.customer_number)}")
    if p.document_status:
        conditions.append(f"FDocumentStatus = {quote_eq(p.document_status)}")
    if p.min_amount is not None:
        conditions.append(f"FBillAllAmount >= {fmt_number(p.min_amount)}")
    if p.bill_no:
        conditions.append(f"FBillNo = {quote_eq(p.bill_no)}")
    if p.order_date:
        conditions.append(f"FDate = {quote_eq(p.order_date)}")
    return " AND ".join(conditions)


SAL_ORDER_TOOL = QueryToolBinding(
    tool_name="kingdee_query_sales_order",
    tool_description=(
        "按条件查询销售订单列表（金蝶 SAL_Order）。"
        "何时用：用户询问销售订单、订单状态、某客户的订单时。"
        "所有参数可选；客户仅支持按编码过滤，只有客户名称时请先用"
        " kingdee_query_customer 查编码。默认返回单据编号/日期/客户/状态/金额；"
        "用户问「用了哪些物料、明细、数量、单价」时必须置 include_entries=true，"
        "返回物料编码/物料名称/数量/含税单价/交货日期（按明细行展开）。"
    ),
    endpoint=SAL_ORDER,
    params_model=SalesOrderParams,
    build_filter=build_sales_order_filter,
    build_field_keys=build_sales_order_field_keys,
)
