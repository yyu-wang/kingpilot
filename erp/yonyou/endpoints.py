"""用友端点描述符示例（「新增厂商三步」的第 2 步）。

字段名取用友 U8/U9 常见命名（``cCode`` / ``dDate`` / ``cVenCode``），
**全部 INFERRED**：未对接真实环境，接入时按实际 OpenAPI 元数据核对。

本文件的价值不是字段准确性，而是展示端点描述符的最小形状：
- 两套字段集：``fields``（可查）+ ``filterable``（可过滤），可查 ≠ 可过滤；
- ``default_field_keys`` 固定返回列，LLM 不能自选任意字段；
- ``default_sort`` 保证分页稳定。
"""

from erp.descriptors import EndpointDescriptor, FieldDescriptor

__all__ = ["PURCHASE_ORDER"]

PURCHASE_ORDER = EndpointDescriptor(
    form_id="PU_PurchaseOrder",
    label="采购订单",
    fields=(
        FieldDescriptor(name="cCode", data_type="string", filterable=True, description="订单编号"),
        FieldDescriptor(name="dDate", data_type="date", filterable=True, description="订单日期"),
        FieldDescriptor(
            name="cVenCode", data_type="string", filterable=True, description="供应商编码"
        ),
        FieldDescriptor(
            name="cDepCode", data_type="string", filterable=True, description="部门编码"
        ),
        FieldDescriptor(name="cStatus", data_type="enum", filterable=True, description="单据状态"),
        FieldDescriptor(name="iAmount", data_type="number", filterable=True, description="金额"),
        FieldDescriptor(name="cMemo", data_type="string", filterable=False, description="备注"),
    ),
    default_field_keys=("cCode", "dDate", "cVenCode", "cStatus", "iAmount"),
    default_sort="cCode",
)
