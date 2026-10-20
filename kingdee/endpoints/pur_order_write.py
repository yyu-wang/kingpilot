"""采购订单下单写工具绑定（采购订单写流程）。

五个工具对应金蝶 ``DynamicFormService`` 的五个动作：

| 工具 | 动作 | 状态迁移 |
|------|------|----------|
| kingdee_create_purchase_order | save | → Z |
| kingdee_submit_purchase_order | submit | Z/A → B |
| kingdee_audit_purchase_order | audit | B → C |
| kingdee_unaudit_purchase_order | unaudit | C → A |
| kingdee_delete_purchase_order | delete | Z/A → 删除 |

与采购入库单的区别：本模块是「向供应商下单」；到货入库请用
``kingdee_create_purchase_inbound``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import Field

from kingdee.endpoints.base import (
    EntityReference,
    WriteRemoteRunner,
    WriteToolBinding,
)
from kingdee.endpoints.pur_order import PURCHASE_ORDER
from models.base import KingPilotBaseModel
from models.kingdee import ActionResult

if TYPE_CHECKING:
    from kingdee.client import KingdeeClient, KingdeeWriteAction

_BILL_NO_PATTERN = r"^PO\d{6}$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_MAX_NOTE_CHARS = 200


class PurchaseOrderCreateParams(KingPilotBaseModel):
    """创建采购订单的结构化参数（沙箱简化为单物料行）。"""

    supplier_number: str = Field(
        min_length=1,
        max_length=64,
        description="供应商编码（如 SUP001）或名称；系统会预检并换成内码",
    )
    material_number: str = Field(
        min_length=1,
        max_length=64,
        description="物料编码（如 MAT001）或名称；系统会预检并换成内码",
    )
    quantity: float = Field(gt=0, le=1_000_000, description="采购数量（大于 0）")
    tax_price: float = Field(gt=0, le=100_000_000, description="含税单价（大于 0）")
    order_date: str = Field(
        default="",
        description=f"单据日期，格式 YYYY-MM-DD；留空取当天。须匹配 {_DATE_PATTERN}",
    )
    note: str = Field(default="", max_length=_MAX_NOTE_CHARS, description="备注")


class PurchaseOrderActionParams(KingPilotBaseModel):
    """采购订单状态流转参数（提交/审核/反审核/删除共用）。"""

    bill_no: str = Field(
        pattern=_BILL_NO_PATTERN,
        description="采购订单编号（如 PO000001）",
    )


def _reference(value: str) -> dict[str, Any]:
    """实体标识 → 金蝶引用字段：纯数字按内码（FID），否则按编码（FNumber）。"""
    text = value.strip()
    return {"FID": int(text)} if text.isdigit() else {"FNumber": text}


async def create_purchase_order(client: KingdeeClient, params: KingPilotBaseModel) -> ActionResult:
    """Save：创建一张暂存采购订单。"""
    p = cast(PurchaseOrderCreateParams, params)
    model: dict[str, Any] = {
        "FSupplierID": _reference(p.supplier_number),
        "FMaterialID": _reference(p.material_number),
        "FQty": p.quantity,
        "FTaxPrice": p.tax_price,
    }
    if p.order_date:
        model["FDate"] = p.order_date
    if p.note:
        model["FNote"] = p.note
    return await client.execute_action("save", PURCHASE_ORDER.form_id, model=model)


def _action_runner(action: KingdeeWriteAction) -> WriteRemoteRunner:
    """生成状态流转执行器（四个动作结构相同）。"""

    async def runner(client: KingdeeClient, params: KingPilotBaseModel) -> ActionResult:
        p = cast(PurchaseOrderActionParams, params)
        return await client.execute_action(action, PURCHASE_ORDER.form_id, bill_no=p.bill_no)

    return runner


_CREATE_DESCRIPTION = (
    "创建采购订单（写操作，需审批），即向供应商下单。"
    "何时用：用户明确要求新建/开出一张采购订单，并给出供应商、物料、数量、单价时。"
    "供应商/物料可给编码或名称，系统会先校验存在性；缺少必填项时先向用户追问。"
    "创建结果为暂存（Z）；到货入库请使用采购入库单工具，不要用本工具。"
)

_ACTION_DESCRIPTIONS: dict[str, str] = {
    "submit": "提交采购订单（写操作，需审批）：暂存/创建（Z/A）→ 审核中（B）。",
    "audit": "审核采购订单（写操作，需审批）：审核中（B）→ 已审核（C）。",
    "unaudit": "反审核采购订单（写操作，需审批）：已审核（C）→ 创建（A）。",
    "delete": "删除采购订单（写操作，需审批）：仅暂存/创建（Z/A）可删除。",
}


CREATE_PURCHASE_ORDER_TOOL = WriteToolBinding(
    tool_name="kingdee_create_purchase_order",
    tool_description=_CREATE_DESCRIPTION,
    endpoint=PURCHASE_ORDER,
    params_model=PurchaseOrderCreateParams,
    action="save",
    runner=create_purchase_order,
    tool_type="write",
    entity_references=(
        EntityReference(parameter="supplier_number", entity_type="supplier"),
        EntityReference(parameter="material_number", entity_type="material"),
    ),
    required_parameters=(
        "supplier_number",
        "material_number",
        "quantity",
        "tax_price",
    ),
)


def _action_binding(action: KingdeeWriteAction) -> WriteToolBinding:
    return WriteToolBinding(
        tool_name=f"kingdee_{action}_purchase_order",
        tool_description=_ACTION_DESCRIPTIONS[action],
        endpoint=PURCHASE_ORDER,
        params_model=PurchaseOrderActionParams,
        action=action,
        runner=_action_runner(action),
        tool_type="action",
        required_parameters=("bill_no",),
    )


SUBMIT_PURCHASE_ORDER_TOOL = _action_binding("submit")
AUDIT_PURCHASE_ORDER_TOOL = _action_binding("audit")
UNAUDIT_PURCHASE_ORDER_TOOL = _action_binding("unaudit")
DELETE_PURCHASE_ORDER_TOOL = _action_binding("delete")

PURCHASE_ORDER_WRITE_TOOLS: tuple[WriteToolBinding, ...] = (
    CREATE_PURCHASE_ORDER_TOOL,
    SUBMIT_PURCHASE_ORDER_TOOL,
    AUDIT_PURCHASE_ORDER_TOOL,
    UNAUDIT_PURCHASE_ORDER_TOOL,
    DELETE_PURCHASE_ORDER_TOOL,
)
