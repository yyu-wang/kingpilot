"""仓库基础资料（BD_Stock）端点描述。

只作为**写操作业务预检**的数据源（采购入库前校验仓库是否存在），
不注册为 LLM 查询工具。

**字段 INFERRED**：与其它端点一致，字段拼写待真环境联调时复核。
"""

from kingdee.endpoints.base import EndpointDescriptor, FieldDescriptor

_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(name="FNumber", data_type="string", filterable=True, description="仓库编码"),
    FieldDescriptor(
        name="FID",
        data_type="number",
        filterable=False,
        description="内部主键（预检用，不可过滤）",
    ),
    FieldDescriptor(name="FName", data_type="string", filterable=True, description="仓库名称"),
)  # INFERRED: 待真环境复核字段名

BD_STOCK = EndpointDescriptor(
    form_id="BD_Stock",
    label="仓库",
    fields=_FIELDS,
    default_field_keys=("FNumber", "FName"),
    default_sort="FNumber",
)
