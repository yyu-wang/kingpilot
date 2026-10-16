"""销售订单（SAL_Order）端点描述（M1-T05）。

字段清单来源：金蝶云星空官方文档与社区高频用法。**无真实环境的限制**——
部分字段拼写未经真环境验证，已用 ``# INFERRED:`` 标注，待 T06b 真环境联调时
用「全字段查询返回列」比对复核。字段名集中在本文件一处，修正只需改一行。

默认查询集与默认排序取最保守值：默认排序用 ``FBillNo`` 而非 ``FDate DESC``，
避免与 M4 Guard 2 的排序字段校验职责重叠。
"""

from kingdee.endpoints.base import EndpointDescriptor, FieldDescriptor

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
