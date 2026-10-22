"""管理后台服务：订单变更审计 + 测试数据集浏览。

设计取舍：
- **不直连沙箱内存库**：当前数据统一走 ``ErpClient.query``，
  与生产/仿真的协议路径一致，切换真环境无需改管理后台。
- **只读**：本服务不提供写操作，避免成为绕过审批与账本的第二入口。
- **过滤在服务端完成**：变更记录按账本字段过滤；数据集按关键字做行级过滤，
  不把原始 FilterString 暴露给前端。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from erp.protocol import ErpClient
from infrastructure.exceptions import KingdeeError
from kingdee.endpoints.base import EndpointDescriptor
from kingdee.endpoints.bd_customer import CUSTOMER
from kingdee.endpoints.bd_material import BD_MATERIAL
from kingdee.endpoints.bd_supplier import SUPPLIER
from kingdee.endpoints.pur_order import PURCHASE_ORDER
from kingdee.endpoints.sal_order import SAL_ORDER
from kingdee.endpoints.stk_inbound import PURCHASE_INBOUND
from kingdee.endpoints.stk_inventory import INVENTORY
from ledger.write_ledger import WriteLedger
from models.admin import (
    AdminOverview,
    ChangeListResponse,
    ChangeRecordView,
    DatasetResponse,
    FieldChange,
)
from models.kingdee import JsonScalar
from models.ledger import WriteRecord

_MAX_DATASET_ROWS = 2_000
_DEFAULT_CHANGE_LIMIT = 200
_MAX_CHANGE_LIMIT = 500

#: 数据集里代表业务时间的字段（按优先级）；命中后列表按时间倒序展示
_TIME_FIELDS = ("FDate", "FCreateDate")


@dataclass(frozen=True)
class DatasetSpec:
    """一个可在管理后台浏览的数据集。"""

    kind: str
    label: str
    endpoint: EndpointDescriptor
    fields: tuple[str, ...]


#: 数据集注册表：kind → 规格。字段顺序即页面列顺序。
DATASETS: dict[str, DatasetSpec] = {
    "sales_orders": DatasetSpec(
        kind="sales_orders",
        label="销售订单",
        endpoint=SAL_ORDER,
        fields=(
            "FBillNo",
            "FDate",
            "FDocumentStatus",
            "FCustomerID.FNumber",
            "FCustomerID.FName",
            "FSalerId.FName",
            "FBillAllAmount",
            "FNote",
        ),
    ),
    "purchase_orders": DatasetSpec(
        kind="purchase_orders",
        label="采购订单",
        endpoint=PURCHASE_ORDER,
        fields=(
            "FBillNo",
            "FDate",
            "FDocumentStatus",
            "FSupplierID.FNumber",
            "FSupplierID.FName",
            "FMaterialId.FNumber",
            "FMaterialId.FName",
            "FQty",
            "FTaxPrice",
            "FAmount",
        ),
    ),
    "purchase_inbounds": DatasetSpec(
        kind="purchase_inbounds",
        label="采购入库单",
        endpoint=PURCHASE_INBOUND,
        fields=(
            "FBillNo",
            "FDate",
            "FDocumentStatus",
            "FSupplierID.FNumber",
            "FSupplierID.FName",
            "FStockId.FName",
            "FMaterialId.FNumber",
            "FMaterialId.FName",
            "FRealQty",
            "FSrcBillNo",
            "FAmount",
        ),
    ),
    "customers": DatasetSpec(
        kind="customers",
        label="客户",
        endpoint=CUSTOMER,
        fields=("FNumber", "FID", "FName", "FShortName"),
    ),
    "suppliers": DatasetSpec(
        kind="suppliers",
        label="供应商",
        endpoint=SUPPLIER,
        fields=("FNumber", "FID", "FName", "FShortName"),
    ),
    "materials": DatasetSpec(
        kind="materials",
        label="物料",
        endpoint=BD_MATERIAL,
        fields=("FNumber", "FID", "FName"),
    ),
    "inventory": DatasetSpec(
        kind="inventory",
        label="即时库存",
        endpoint=INVENTORY,
        fields=("FMaterialId.FNumber", "FMaterialId.FName", "FStockId.FName", "FBaseQty"),
    ),
}


class AdminConsoleService:
    """管理后台只读查询服务。"""

    def __init__(self, client: ErpClient, ledger: WriteLedger) -> None:
        self._client = client
        self._ledger = ledger

    def changes(
        self,
        *,
        form_id: str = "",
        action: str = "",
        status: str = "",
        query: str = "",
        limit: int = _DEFAULT_CHANGE_LIMIT,
    ) -> ChangeListResponse:
        """按条件返回写操作记录（时间倒序）。"""
        resolved_limit = min(max(limit, 1), _MAX_CHANGE_LIMIT)
        keyword = query.strip().lower()
        ordered = sorted(self._ledger.records(), key=lambda record: record.timestamp, reverse=True)
        items = [
            _to_view(record)
            for record in ordered
            if _matches(record.form_id, form_id)
            and _matches(record.action, action)
            and _matches(record.status, status)
            and _matches_keyword(record, keyword)
        ]
        return ChangeListResponse(
            total=len(items),
            chain_valid=self._ledger.verify().valid,
            items=items[:resolved_limit],
        )

    async def dataset(
        self,
        kind: str,
        *,
        query: str = "",
        limit: int = _MAX_DATASET_ROWS,
    ) -> DatasetResponse:
        """查询一个数据集；关键字在返回行上做大小写不敏感匹配。"""
        spec = DATASETS.get(kind)
        if spec is None:
            raise KeyError(kind)
        resolved_limit = min(max(limit, 1), _MAX_DATASET_ROWS)
        all_rows: list[dict[str, JsonScalar]] = []
        # BillQuerySpec.limit 上限 100 是 LLM 可见面约束；管理后台为内部特权
        # 读取方，按 100 一页内部翻页直到凑够上限或读到短页（无更多数据）
        while len(all_rows) < resolved_limit:
            page = await self._client.query(
                spec.endpoint.to_query_params(
                    field_keys=spec.fields,
                    order_by=_time_order(spec.fields),
                    start_row=len(all_rows),
                    limit=min(100, resolved_limit - len(all_rows)),
                )
            )
            all_rows.extend(page.rows)
            if len(page.rows) < 100:
                break
        keyword = query.strip().lower()
        rows = [
            row
            for row in all_rows
            if not keyword or any(keyword in str(value).lower() for value in row.values())
        ]
        return DatasetResponse(
            kind=spec.kind,
            label=spec.label,
            form_id=spec.endpoint.form_id,
            fields=list(spec.fields),
            rows=rows,
            total=len(rows),
        )

    async def overview(self) -> AdminOverview:
        """总览：账本统计 + 各数据集行数（查询失败记为 -1）。"""
        records = self._ledger.records()
        by_form: dict[str, int] = {}
        by_action: dict[str, int] = {}
        for record in records:
            by_form[record.form_id] = by_form.get(record.form_id, 0) + 1
            by_action[record.action] = by_action.get(record.action, 0) + 1

        counts = await asyncio.gather(*(self._count_dataset(spec) for spec in DATASETS.values()))
        return AdminOverview(
            changes_total=len(records),
            changes_success=sum(1 for record in records if record.status == "success"),
            changes_failed=sum(1 for record in records if record.status == "failed"),
            by_form=by_form,
            by_action=by_action,
            datasets=dict(counts),
            chain_valid=self._ledger.verify().valid,
            generated_at=datetime.now(),
        )

    async def _count_dataset(self, spec: DatasetSpec) -> tuple[str, int]:
        try:
            dataset = await self.dataset(spec.kind)
        except KingdeeError as exc:
            logger.warning("管理后台数据集查询失败: kind={} error={}", spec.kind, exc)
            return spec.kind, -1
        return spec.kind, dataset.total


def _time_order(fields: tuple[str, ...]) -> str | None:
    """数据集的时间排序串（新→旧）；无时间字段时返回 None 走端点默认排序。"""
    time_field = next((name for name in _TIME_FIELDS if name in fields), "")
    if not time_field:
        return None
    keys = [f"{time_field} DESC"]
    if "FBillNo" in fields:
        # 同一天内单号更大的更晚创建，作为稳定的次级排序
        keys.append("FBillNo DESC")
    return ",".join(keys)


def _matches(value: str, expected: str) -> bool:
    return not expected or value == expected


def _matches_keyword(record: WriteRecord, keyword: str) -> bool:
    if not keyword:
        return True
    text = " ".join(
        [
            record.business_key,
            record.form_id,
            record.trace_id,
            record.provenance.user_id,
        ]
    )
    return keyword in text.lower()


def _to_view(record: WriteRecord) -> ChangeRecordView:
    provenance = record.provenance
    return ChangeRecordView(
        record_id=record.record_id,
        timestamp=record.timestamp,
        trace_id=record.trace_id,
        form_id=record.form_id,
        business_key=record.business_key,
        action=record.action,
        status=record.status,
        user_id=provenance.user_id,
        risk_score=provenance.risk_score,
        risk_level=provenance.risk_level,
        approval_chain=list(provenance.approval_chain),
        user_input=provenance.user_original_input,
        tool_params=dict(provenance.tool_call_params),
        api_response=dict(provenance.api_response),
        field_diff=[
            FieldChange(
                field_name=item.field_name,
                old_value=item.old_value,
                new_value=item.new_value,
            )
            for item in record.field_diff
        ],
        before_snapshot=_optional_dict(record.before_snapshot),
        after_snapshot=_optional_dict(record.after_snapshot),
        record_hash=record.record_hash,
    )


def _optional_dict(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None
