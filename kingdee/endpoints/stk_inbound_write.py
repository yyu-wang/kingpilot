"""采购入库单写工具绑定（采购入库写流程）。

五个工具对应金蝶 ``DynamicFormService`` 的五个动作：

| 工具 | 动作 | 状态迁移 |
|------|------|----------|
| erp_create_purchase_inbound | save | → Z |
| erp_submit_purchase_inbound | submit | Z/A → B |
| erp_audit_purchase_inbound | audit | B → C |
| erp_unaudit_purchase_inbound | unaudit | C → A |
| erp_delete_purchase_inbound | delete | Z/A → 删除 |

业务约束（由沙箱入库库在写入时校验）：
- 源采购订单必须存在且已审核（C）
- 供应商必须与源采购订单一致
- 物料与仓库必须存在
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import Field

from erp.tool_names import CREATE_PURCHASE_INBOUND, OBJ_PURCHASE_INBOUND, WRITE_TOOL_NAMES
from kingdee.endpoints.base import (
    EntityReference,
    WriteRemoteRunner,
    WriteToolBinding,
)
from kingdee.endpoints.stk_inbound import PURCHASE_INBOUND
from models.base import KingPilotBaseModel
from models.kingdee import ActionResult

if TYPE_CHECKING:
    from erp.protocol import ErpClient
    from kingdee.client import KingdeeWriteAction

_BILL_NO_PATTERN = r"^CGRK\d{6}$"
_SOURCE_BILL_PATTERN = r"^PO\d{6}$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_MAX_NOTE_CHARS = 200


class PurchaseInboundCreateParams(KingPilotBaseModel):
    """创建采购入库单的结构化参数。"""

    source_bill_no: str = Field(
        pattern=_SOURCE_BILL_PATTERN,
        description="源采购订单号（如 PO000001）；该采购订单必须已审核",
    )
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
    warehouse_name: str = Field(
        min_length=1,
        max_length=64,
        description="仓库名称（如 成品仓 / 原料仓 / 半成品仓）",
    )
    quantity: float = Field(gt=0, le=1_000_000, description="实收数量（大于 0）")
    tax_price: float | None = Field(
        default=None,
        gt=0,
        le=100_000_000,
        description="含税单价（可选）；提供时用于计算入库金额与风险评分",
    )
    inbound_date: str = Field(
        default="",
        description=f"入库日期，格式 YYYY-MM-DD；留空取当天。须匹配 {_DATE_PATTERN}",
    )
    note: str = Field(default="", max_length=_MAX_NOTE_CHARS, description="备注")


class PurchaseInboundActionParams(KingPilotBaseModel):
    """采购入库单状态流转参数（提交/审核/反审核/删除共用）。"""

    bill_no: str = Field(
        pattern=_BILL_NO_PATTERN,
        description="采购入库单编号（如 CGRK000001）",
    )


def _reference(value: str) -> dict[str, Any]:
    """实体标识 → 金蝶引用字段：纯数字按内码（FID），否则按编码（FNumber）。"""
    text = value.strip()
    return {"FID": int(text)} if text.isdigit() else {"FNumber": text}


async def create_purchase_inbound(client: ErpClient, params: KingPilotBaseModel) -> ActionResult:
    """Save：创建一张暂存采购入库单。"""
    p = cast(PurchaseInboundCreateParams, params)
    model: dict[str, Any] = {
        "FSrcBillNo": p.source_bill_no,
        "FSupplierID": _reference(p.supplier_number),
        "FMaterialID": _reference(p.material_number),
        "FStockId": {"FName": p.warehouse_name},
        "FRealQty": p.quantity,
    }
    if p.tax_price is not None:
        model["FTaxPrice"] = p.tax_price
    if p.inbound_date:
        model["FDate"] = p.inbound_date
    if p.note:
        model["FNote"] = p.note
    return await client.invoke_action("save", PURCHASE_INBOUND.form_id, model=model)


def _action_runner(action: KingdeeWriteAction) -> WriteRemoteRunner:
    """生成状态流转执行器（四个动作结构相同）。"""

    async def runner(client: ErpClient, params: KingPilotBaseModel) -> ActionResult:
        p = cast(PurchaseInboundActionParams, params)
        return await client.invoke_action(action, PURCHASE_INBOUND.form_id, bill_no=p.bill_no)

    return runner


_CREATE_DESCRIPTION = (
    "创建采购入库单（写操作，需审批），即采购订单到货后仓库实际收货入库。"
    "何时用：用户明确要求对某采购订单做入库/收货/到货登记，并给出源采购订单号、"
    "供应商、物料、仓库、实收数量时。"
    "源采购订单必须已审核且供应商一致；缺少必填项时先向用户追问。"
    "源单状态、供应商一致性与仓库存在性由系统在服务端预检，"
    "无需先调用查询工具核对，直接调用本工具即可。"
    "创建结果为暂存（Z），需要提交/审核请继续调用对应工具。"
)

_ACTION_DESCRIPTIONS: dict[str, str] = {
    "submit": "提交采购入库单（写操作，需审批）：暂存/创建（Z/A）→ 审核中（B）。",
    "audit": "审核采购入库单（写操作，需审批）：审核中（B）→ 已审核（C，已入库）。",
    "unaudit": "反审核采购入库单（写操作，需审批）：已审核（C）→ 创建（A）。",
    "delete": "删除采购入库单（写操作，需审批）：仅暂存/创建（Z/A）可删除。",
}


CREATE_PURCHASE_INBOUND_TOOL = WriteToolBinding(
    tool_name=CREATE_PURCHASE_INBOUND,
    tool_description=_CREATE_DESCRIPTION,
    endpoint=PURCHASE_INBOUND,
    params_model=PurchaseInboundCreateParams,
    action="save",
    runner=create_purchase_inbound,
    tool_type="write",
    entity_references=(
        EntityReference(parameter="supplier_number", entity_type="supplier"),
        EntityReference(parameter="material_number", entity_type="material"),
    ),
    required_parameters=(
        "source_bill_no",
        "supplier_number",
        "material_number",
        "warehouse_name",
        "quantity",
    ),
)


def _action_binding(action: KingdeeWriteAction) -> WriteToolBinding:
    return WriteToolBinding(
        tool_name=WRITE_TOOL_NAMES[(OBJ_PURCHASE_INBOUND, action)],
        tool_description=_ACTION_DESCRIPTIONS[action],
        endpoint=PURCHASE_INBOUND,
        params_model=PurchaseInboundActionParams,
        action=action,
        runner=_action_runner(action),
        tool_type="action",
        required_parameters=("bill_no",),
    )


SUBMIT_PURCHASE_INBOUND_TOOL = _action_binding("submit")
AUDIT_PURCHASE_INBOUND_TOOL = _action_binding("audit")
UNAUDIT_PURCHASE_INBOUND_TOOL = _action_binding("unaudit")
DELETE_PURCHASE_INBOUND_TOOL = _action_binding("delete")

PURCHASE_INBOUND_WRITE_TOOLS: tuple[WriteToolBinding, ...] = (
    CREATE_PURCHASE_INBOUND_TOOL,
    SUBMIT_PURCHASE_INBOUND_TOOL,
    AUDIT_PURCHASE_INBOUND_TOOL,
    UNAUDIT_PURCHASE_INBOUND_TOOL,
    DELETE_PURCHASE_INBOUND_TOOL,
)
