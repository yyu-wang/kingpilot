"""实体抽取器（M3-T04，方案 §5.3 支柱九 Layer 3）。

职责：把工具结构化结果（``BillQueryResult.model_dump()``）里的金蝶业务实体
（客户/订单/供应商/物料）抽成 ``EntityRef``，供 ``MemoryStore.upsert_entity``
写入实体图。

设计要点：
- **字段映射表驱动**（D2）：每个 form_id 对应一组 ``EntitySpec``，一表单可
  同时产出多种实体（如 SAL_Order 同时产 order + 关联 customer）。
- **内码优先、名称兜底**：id 字段和 name 字段都取不到值时跳过（无法锚定）；
  id 空时用 name 作 ``display_name`` 占位，后续查到内码由 ``upsert_entity``
  的 (type, id or name) 去重键合并。
- **未知表单保守返回空**：不在映射表里的 form_id 不抽取，避免误建实体。
- **与 endpoint 字段定义对齐**：映射表集中在本文件，字段名变更只改一处。
"""

from dataclasses import dataclass
from typing import Any

from models.memory import EntityRef


@dataclass(frozen=True)
class EntitySpec:
    """单类实体的抽取规格：从一行结果里取哪个字段当 id / name / attributes。"""

    entity_type: str
    id_field: str  # 金蝶内码字段（如 FNumber / FBillNo / FCustomerID.FNumber）
    name_field: str  # 显示名称字段；与 id_field 相同时单据编号即标识
    attrs: tuple[str, ...] = ()  # 额外入 attributes 的字段（非 None 才入）


# 表单 → 抽取规格列表（一表单可产多实体，关联字段从同一行里拎出对方实体）
EXTRACT_SPECS: dict[str, tuple[EntitySpec, ...]] = {
    "BD_Customer": (
        EntitySpec(
            entity_type="customer",
            id_field="FNumber",
            name_field="FName",
            attrs=("FShortName",),
        ),
    ),
    "BD_Supplier": (
        EntitySpec(
            entity_type="supplier",
            id_field="FNumber",
            name_field="FName",
            attrs=("FShortName",),
        ),
    ),
    "SAL_Order": (
        EntitySpec(
            entity_type="order",
            id_field="FBillNo",
            name_field="FBillNo",  # 销售订单无独立名称，单据编号即标识
            attrs=("FDate", "FDocumentStatus", "FBillAllAmount"),
        ),
        EntitySpec(
            entity_type="customer",
            id_field="FCustomerID.FNumber",
            name_field="FCustomerID.FName",
        ),
    ),
    "PUR_PurchaseOrder": (
        EntitySpec(
            entity_type="order",
            id_field="FBillNo",
            name_field="FBillNo",
            attrs=("FDate", "FDocumentStatus", "FAmount"),
        ),
        EntitySpec(
            entity_type="supplier",
            id_field="FSupplierID.FNumber",
            name_field="FSupplierID.FName",
        ),
    ),
    "STK_Inventory": (
        EntitySpec(
            entity_type="material",
            id_field="FMaterialId.FNumber",
            name_field="FMaterialId.FName",
            attrs=("FStockId.FName", "FBaseQty"),
        ),
    ),
}


def extract_entities(result: dict[str, Any], round_no: int) -> list[EntityRef]:
    """从一条工具结构化结果中抽取实体列表。

    Args:
        result: ``BillQueryResult.model_dump()`` 形态，必须含 ``form_id`` 与
            ``rows``；缺字段或未知 form_id 返回空列表（保守不抽）。
        round_no: 当前对话轮次，写入 ``last_mentioned_in_round``。

    Returns:
        抽取到的实体；同 result 可能产出多个类型的实体（如订单行里的订单+客户）。
    """
    form_id = result.get("form_id")
    rows = result.get("rows")
    if not isinstance(form_id, str) or not isinstance(rows, list):
        return []
    specs = EXTRACT_SPECS.get(form_id)
    if specs is None:
        return []

    entities: list[EntityRef] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for spec in specs:
            entity = _entity_from_row(spec, row, round_no)
            if entity is not None:
                entities.append(entity)
    return entities


def _entity_from_row(spec: EntitySpec, row: dict[str, Any], round_no: int) -> EntityRef | None:
    """单行按 spec 抽一个实体；id 和 name 都取不到值时返回 None。"""
    kingdee_id = _as_str(row.get(spec.id_field))
    name = _as_str(row.get(spec.name_field))
    if not kingdee_id and not name:
        return None
    attributes = {attr: row[attr] for attr in spec.attrs if attr in row and row[attr] is not None}
    return EntityRef(
        entity_type=spec.entity_type,
        display_name=name or kingdee_id,
        kingdee_id=kingdee_id,
        last_mentioned_in_round=round_no,
        attributes=attributes,
    )


def _as_str(value: Any) -> str:
    """单元格值转字符串；None / 空串 / 0 都视为无值（0 不是有效内码）。"""
    if value is None:
        return ""
    text = str(value).strip()
    return text if text else ""
