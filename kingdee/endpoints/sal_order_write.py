"""销售订单写工具绑定（M9-T13）。

五个工具对应金蝶 ``DynamicFormService`` 的五个动作：

| 工具 | 动作 | 状态迁移 |
|------|------|----------|
| kingdee_create_sales_order | save | → Z |
| kingdee_submit_sales_order | submit | Z/A → B |
| kingdee_audit_sales_order | audit | B → C |
| kingdee_unaudit_sales_order | unaudit | C → A |
| kingdee_delete_sales_order | delete | Z/A → 删除 |

写入参数只在这里组装成金蝶 ``Model`` 报文；LLM 永远接触不到原始报文。
客户/物料经 Guard 3 预检后已换成内码（FID），这里按「数字 = FID、其它 = 编码」
组装，保证被单独调用（绕过 Guard 的测试/脚本）时仍然可用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic import Field

from kingdee.endpoints.base import (
    EntityReference,
    WriteRemoteRunner,
    WriteToolBinding,
)
from kingdee.endpoints.sal_order import SAL_ORDER
from models.base import KingPilotBaseModel
from models.kingdee import ActionResult

if TYPE_CHECKING:
    from kingdee.client import KingdeeClient, KingdeeWriteAction

_BILL_NO_PATTERN = r"^SO\d{6}$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_MAX_NOTE_CHARS = 200


class SalesOrderCreateParams(KingPilotBaseModel):
    """创建销售订单的结构化参数。"""

    customer_number: str = Field(
        min_length=1,
        max_length=64,
        description="客户编码（如 CUS001）或客户名称；系统会预检并换成内码",
    )
    material_number: str = Field(
        min_length=1,
        max_length=64,
        description="物料编码（如 MAT001）或物料名称；系统会预检并换成内码",
    )
    quantity: float = Field(gt=0, le=1_000_000, description="数量（大于 0）")
    tax_price: float = Field(gt=0, le=100_000_000, description="含税单价（大于 0）")
    order_date: str = Field(
        default="",
        description=f"单据日期，格式 YYYY-MM-DD（如 2026-10-19）；留空取当天。须匹配 {_DATE_PATTERN}",
    )
    note: str = Field(default="", max_length=_MAX_NOTE_CHARS, description="备注")


class SalesOrderActionParams(KingPilotBaseModel):
    """销售订单状态流转参数（提交/审核/反审核/删除共用）。"""

    bill_no: str = Field(
        pattern=_BILL_NO_PATTERN,
        description="销售订单编号（如 SO000004）",
    )


def _reference(value: str) -> dict[str, Any]:
    """实体标识 → 金蝶引用字段：纯数字按内码（FID），否则按编码（FNumber）。"""
    text = value.strip()
    return {"FID": int(text)} if text.isdigit() else {"FNumber": text}


async def create_sales_order(client: KingdeeClient, params: KingPilotBaseModel) -> ActionResult:
    """Save：创建一张暂存销售订单。"""
    p = cast(SalesOrderCreateParams, params)
    model: dict[str, Any] = {
        "FCustomerID": _reference(p.customer_number),
        "FMaterialID": _reference(p.material_number),
        "FQty": p.quantity,
        "FTaxPrice": p.tax_price,
    }
    if p.order_date:
        model["FDate"] = p.order_date
    if p.note:
        model["FNote"] = p.note
    return await client.execute_action("save", SAL_ORDER.form_id, model=model)


def _action_runner(action: KingdeeWriteAction) -> WriteRemoteRunner:
    """生成状态流转执行器（四个动作结构相同，避免四份复制粘贴）。"""

    async def runner(client: KingdeeClient, params: KingPilotBaseModel) -> ActionResult:
        p = cast(SalesOrderActionParams, params)
        return await client.execute_action(action, SAL_ORDER.form_id, bill_no=p.bill_no)

    return runner


_CREATE_DESCRIPTION = (
    "创建销售订单（写操作，需审批）。"
    "何时用：用户明确要求新建/开具一张销售订单，并给出客户、物料、数量、单价时。"
    "客户/物料可给编码或名称，系统会先校验存在性；缺少必填项时先向用户追问。"
    "创建结果为暂存状态（Z），需要提交/审核请继续调用对应工具。"
)

_ACTION_DESCRIPTIONS: dict[str, str] = {
    "submit": "提交销售订单（写操作，需审批）：暂存/创建（Z/A）→ 审核中（B）。",
    "audit": "审核销售订单（写操作，需审批）：审核中（B）→ 已审核（C）。",
    "unaudit": "反审核销售订单（写操作，需审批）：已审核（C）→ 创建（A）。",
    "delete": "删除销售订单（写操作，需审批）：仅暂存/创建（Z/A）可删除。",
}


CREATE_SALES_ORDER_TOOL = WriteToolBinding(
    tool_name="kingdee_create_sales_order",
    tool_description=_CREATE_DESCRIPTION,
    endpoint=SAL_ORDER,
    params_model=SalesOrderCreateParams,
    action="save",
    runner=create_sales_order,
    tool_type="write",
    entity_references=(
        EntityReference(parameter="customer_number", entity_type="customer"),
        EntityReference(parameter="material_number", entity_type="material"),
    ),
    required_parameters=("customer_number", "material_number", "quantity", "tax_price"),
)


def _action_binding(action: KingdeeWriteAction) -> WriteToolBinding:
    """四个状态流转工具的绑定（动作名即工具名后缀）。"""
    return WriteToolBinding(
        tool_name=f"kingdee_{action}_sales_order",
        tool_description=_ACTION_DESCRIPTIONS[action],
        endpoint=SAL_ORDER,
        params_model=SalesOrderActionParams,
        action=action,
        runner=_action_runner(action),
        tool_type="action",
        required_parameters=("bill_no",),
    )


SUBMIT_SALES_ORDER_TOOL = _action_binding("submit")
AUDIT_SALES_ORDER_TOOL = _action_binding("audit")
UNAUDIT_SALES_ORDER_TOOL = _action_binding("unaudit")
DELETE_SALES_ORDER_TOOL = _action_binding("delete")

SALES_ORDER_WRITE_TOOLS: tuple[WriteToolBinding, ...] = (
    CREATE_SALES_ORDER_TOOL,
    SUBMIT_SALES_ORDER_TOOL,
    AUDIT_SALES_ORDER_TOOL,
    UNAUDIT_SALES_ORDER_TOOL,
    DELETE_SALES_ORDER_TOOL,
)
