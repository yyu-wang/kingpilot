"""物料基础资料（BD_Material）端点描述（M9-T13）。

只作为**写操作实体预检**的数据源（Guard 3 把物料编码/名称换成内码），
不注册为 LLM 查询工具——物料查询需求仍走 ``kingdee_query_inventory``。

**全部字段 INFERRED**：与其它端点一致，字段拼写待真环境联调时复核。
"""

from kingdee.endpoints.base import EndpointDescriptor, FieldDescriptor

_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(name="FNumber", data_type="string", filterable=True, description="物料编码"),
    FieldDescriptor(
        name="FID",
        data_type="number",
        filterable=False,
        description="内部主键（写操作实体引用预检用，不可过滤）",
    ),
    FieldDescriptor(name="FName", data_type="string", filterable=True, description="物料名称"),
)  # INFERRED: 待真环境复核字段名

BD_MATERIAL = EndpointDescriptor(
    form_id="BD_Material",
    label="物料",
    fields=_FIELDS,
    default_field_keys=("FNumber", "FName"),
    default_sort="FNumber",
)
