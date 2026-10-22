"""写操作快照（M9-T13 / 采购入库写流程）。

按 ``bill_no`` 查询单据当前行，作为写前/写后快照：

- 状态流转（submit/audit/unaudit/delete）：写前查一次、写后再查一次
- 创建（save）：执行前单号未知，``before``/``after`` 均为 None；
  真实单号由写入结果（``ActionResult.bill_no``）进入 ``api_response``

支持的表单：销售订单（SAL_Order）与采购入库单（STK_InStock）；
未知写工具返回 None（WriteCoordinator 对 edit/unaudit/delete 要求写前快照，
缺快照会 fail-closed）。
"""

from collections.abc import Mapping
from typing import Any

from erp import FilterExpression, eq
from erp.protocol import ErpClient
from infrastructure.exceptions import KingdeeError
from kingdee.endpoints.base import EndpointDescriptor
from kingdee.endpoints.pur_order import PURCHASE_ORDER
from kingdee.endpoints.sal_order import SAL_ORDER
from kingdee.endpoints.stk_inbound import PURCHASE_INBOUND
from models.kingdee import JsonScalar

_SALES_FIELDS = ("FBillNo", "FDate", "FDocumentStatus", "FCustomerID.FNumber", "FBillAllAmount")
_PURCHASE_FIELDS = (
    "FBillNo",
    "FDate",
    "FDocumentStatus",
    "FSupplierID.FNumber",
    "FAmount",
    "FMaterialId.FNumber",
    "FQty",
)
_INBOUND_FIELDS = (
    "FBillNo",
    "FDate",
    "FDocumentStatus",
    "FSupplierID.FNumber",
    "FStockId.FName",
    "FMaterialId.FNumber",
    "FRealQty",
)

# 工具名关键字 → (端点, 快照字段)；避免逐一枚举 15 个工具
_SNAPSHOT_TARGETS: tuple[tuple[str, EndpointDescriptor, tuple[str, ...]], ...] = (
    ("purchase_inbound", PURCHASE_INBOUND, _INBOUND_FIELDS),
    ("purchase_order", PURCHASE_ORDER, _PURCHASE_FIELDS),
    ("sales_order", SAL_ORDER, _SALES_FIELDS),
)


class KingdeeSnapshotProvider:
    """基于 ExecuteBillQuery 的单据快照实现。"""

    def __init__(self, client: ErpClient) -> None:
        self._client = client

    async def before(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> dict[str, JsonScalar] | None:
        """写前快照：按单号查当前单据；无单号（创建）返回 None。"""
        return await self._fetch(tool_name, arguments)

    async def after(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> dict[str, JsonScalar] | None:
        """写后快照：按单号查最新状态；删除后查不到返回 None。"""
        return await self._fetch(tool_name, arguments)

    async def _fetch(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> dict[str, JsonScalar] | None:
        bill_no = arguments.get("bill_no")
        if not isinstance(bill_no, str) or not bill_no.strip():
            return None
        target = _target_for(tool_name)
        if target is None:
            return None
        endpoint, field_keys = target
        try:
            condition = eq("FBillNo", bill_no.strip())
        except ValueError as exc:
            raise KingdeeError("单号包含非法字符", context={"length": len(bill_no)}) from exc
        spec = endpoint.to_query_params(
            field_keys=field_keys,
            filter=FilterExpression(conditions=(condition,)),
            limit=1,
        )
        result = await self._client.query(spec)
        return result.rows[0] if result.rows else None


def _target_for(tool_name: str) -> tuple[EndpointDescriptor, tuple[str, ...]] | None:
    for keyword, endpoint, fields in _SNAPSHOT_TARGETS:
        if keyword in tool_name:
            return endpoint, fields
    return None
