"""业务一致性预检（采购入库）：源单状态 / 供应商一致 / 仓库存在。

Why 独立于 Guard 3：
- Guard 3 只回答「实体是否存在」，不校验单据状态与跨实体一致性；
- 这些规则若只在执行期校验，用户会先拿到审批单、审批通过后才看到失败。

位置：在实体换码之前执行，直接比较用户原始输入（编码或名称）。
任何查询失败都按拦截处理（fail-closed），避免带着不确定状态创建审批单。
"""

from collections.abc import Sequence
from typing import Any

from erp import FilterCondition, FilterExpression, eq
from erp.protocol import ErpClient
from infrastructure.exceptions import KingPilotError, ToolError
from kingdee.endpoints.base import EndpointDescriptor
from kingdee.endpoints.bd_stock import BD_STOCK
from kingdee.endpoints.pur_order import PURCHASE_ORDER
from models.kingdee import JsonScalar
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_business"

_CREATE_INBOUND_TOOL = "erp_create_purchase_inbound"
#: 源采购订单允许入库的状态：仅已审核（C）
_AUDITED_STATUS = "C"


class InboundPreflightGuard:
    """采购入库创建前的业务一致性校验。"""

    name = GUARD_NAME

    def __init__(self, client: ErpClient) -> None:
        self._client = client

    async def check(self, context: GuardContext) -> GuardResult:
        """只处理采购入库创建；其他工具直接放行。"""
        if context.tool_name != _CREATE_INBOUND_TOOL:
            return GuardResult.allow(
                detail={"skipped": True, "reason": "非采购入库创建，无需业务预检"}
            )

        source = _text(context.arguments.get("source_bill_no"))
        supplier = _text(context.arguments.get("supplier_number"))
        warehouse = _text(context.arguments.get("warehouse_name"))
        if not source:
            # 必填项缺失由 ParamGuard 负责，这里不重复报错
            return GuardResult.allow(detail={"skipped": True, "reason": "缺少源单号"})

        try:
            orders = await self._query(
                PURCHASE_ORDER,
                (eq("FBillNo", source),),
                ("FBillNo", "FDocumentStatus", "FSupplierID.FNumber", "FSupplierID.FName"),
            )
        except (KingPilotError, ToolError) as exc:
            return GuardResult.block(
                "源采购订单预检查询失败，请稍后重试",
                detail={"branch": "lookup_error", "error_type": type(exc).__name__},
            )

        if not orders:
            return GuardResult.block(
                f"源采购订单不存在：{source}",
                detail={"branch": "source_missing", "source_bill_no": source},
            )

        order = orders[0]
        status = _text(order.get("FDocumentStatus")).upper()
        if status != _AUDITED_STATUS:
            return GuardResult.block(
                f"源采购订单未审核（当前状态 {status or '未知'}），不能入库：{source}",
                detail={
                    "branch": "source_not_audited",
                    "source_bill_no": source,
                    "document_status": status,
                },
            )

        if supplier:
            supplier_number = _text(order.get("FSupplierID.FNumber"))
            supplier_name = _text(order.get("FSupplierID.FName"))
            if supplier not in {supplier_number, supplier_name}:
                expected = supplier_name or supplier_number
                return GuardResult.block(
                    f"供应商与源采购订单不一致：{source} 的供应商为 {expected or '未知'}",
                    detail={
                        "branch": "supplier_mismatch",
                        "source_bill_no": source,
                        "expected": expected,
                    },
                )

        if warehouse:
            try:
                stocks = await self._query(
                    BD_STOCK, (eq("FName", warehouse),), ("FNumber", "FName")
                )
            except (KingPilotError, ToolError) as exc:
                return GuardResult.block(
                    "仓库预检查询失败，请稍后重试",
                    detail={"branch": "lookup_error", "error_type": type(exc).__name__},
                )
            if not stocks:
                return GuardResult.block(
                    f"仓库不存在：{warehouse}",
                    detail={"branch": "warehouse_missing", "warehouse": warehouse},
                )

        return GuardResult.allow(
            detail={
                "branch": "all_valid",
                "source_bill_no": source,
                "document_status": status,
            }
        )

    async def _query(
        self,
        endpoint: EndpointDescriptor,
        conditions: Sequence[FilterCondition],
        fields: Sequence[str],
    ) -> list[dict[str, JsonScalar]]:
        """按端点描述符查一次（limit=1），返回原始行。"""
        params = endpoint.to_query_params(
            field_keys=tuple(fields),
            filter=FilterExpression(conditions=tuple(conditions)),
            limit=1,
        )
        result = await self._client.query(params)
        return list(result.rows)


def _text(value: Any) -> str:
    """参数值 → 去空白字符串；非标量返回空串。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""
