"""实体存在性解析（M4-T04）：写操作引用的客户/供应商/订单先查再放行。

职责边界：本模块只回答「这个实体在系统里是否存在、内码是什么」，
不做拦截决策（归 ``safety/guard_entity.py``），也不调用 LLM。

解析顺序（客户/供应商）：
1. 内码精确匹配（``FID = ...``，标识为纯数字时）
2. 业务编号精确匹配（``FNumber = ...``）
3. 名称精确匹配（``FName = ...``）；命中多条视为**无法确认**，返回候选
4. 名称模糊候选（``FName LIKE ...``，最多 3 条）——供「全部幻觉」分支提示用户

``canonical_id`` 统一为金蝶内码 ``FID``（Guard 5 要求引用字段必须是内码 Id），
业务编号与名称分别放在 ``number`` / ``display_name`` 里供展示与候选提示。

已知限制：
- ``material`` 由 ``BD_Material`` 端点提供（M9-T13 新增；字段拼写 INFERRED）。
- ``order`` 仅覆盖销售订单（SAL_Order）；采购订单另立实体类型后再补。
"""

from dataclasses import dataclass
from typing import Protocol

from infrastructure.exceptions import KingdeeError
from kingdee.client import KingdeeClient
from kingdee.endpoints.base import EndpointDescriptor, quote_eq, quote_like
from kingdee.endpoints.bd_customer import CUSTOMER
from kingdee.endpoints.bd_material import BD_MATERIAL
from kingdee.endpoints.bd_supplier import SUPPLIER
from kingdee.endpoints.sal_order import SAL_ORDER
from models.kingdee import JsonScalar

# 模糊候选条数：够用户辨认，又不让提示词膨胀
_SUGGESTION_LIMIT = 3
# 名称精确匹配时多取一条用于判重（2 条即可判定「有歧义」）
_AMBIGUITY_LIMIT = 2


@dataclass(frozen=True)
class EntityLookupResult:
    """单次实体解析结果。

    ``canonical_id`` 为金蝶内码（FID），``number`` 为业务编号（FNumber/FBillNo）。
    ``candidates`` 是名称/编号的人类可读描述，仅在未命中或有歧义时填充。
    """

    found: bool
    canonical_id: str = ""
    display_name: str = ""
    candidates: tuple[str, ...] = ()
    number: str = ""


class EntityLookup(Protocol):
    """实体解析协议（Guard 3 依赖接口，测试用 fake 实现）。"""

    async def lookup(
        self, entity_type: str, identifier: str, *, allow_name: bool = True
    ) -> EntityLookupResult:
        """解析实体；不支持的实体类型应抛 KingPilotError（fail-closed）。"""
        ...


@dataclass(frozen=True)
class _EntitySpec:
    """实体类型 → 查询契约。"""

    endpoint: EndpointDescriptor
    id_field: str  # 金蝶内码（FID）
    number_field: str  # 业务编号（FNumber / FBillNo）
    name_field: str  # 显示名称（FName / FBillNo）


_ENTITY_SPECS: dict[str, _EntitySpec] = {
    "customer": _EntitySpec(
        endpoint=CUSTOMER, id_field="FID", number_field="FNumber", name_field="FName"
    ),
    "supplier": _EntitySpec(
        endpoint=SUPPLIER, id_field="FID", number_field="FNumber", name_field="FName"
    ),
    "material": _EntitySpec(
        endpoint=BD_MATERIAL, id_field="FID", number_field="FNumber", name_field="FName"
    ),
    # 销售订单无独立名称字段，单据编号即标识
    "order": _EntitySpec(
        endpoint=SAL_ORDER, id_field="FID", number_field="FBillNo", name_field="FBillNo"
    ),
}


class KingdeeEntityLookup:
    """基于金蝶 ExecuteBillQuery 的实体解析实现（只读，无缓存）。"""

    def __init__(self, client: KingdeeClient) -> None:
        self._client = client

    async def lookup(
        self, entity_type: str, identifier: str, *, allow_name: bool = True
    ) -> EntityLookupResult:
        """按「内码 → 编号 → 名称 → 模糊候选」解析；异常归一为 KingdeeError。

        Raises:
            KingdeeError: 不支持的实体类型、标识含非法字符、下游查询失败。
        """
        spec = _ENTITY_SPECS.get(entity_type)
        if spec is None:
            raise KingdeeError(
                f"不支持的实体类型: {entity_type}", context={"entity_type": entity_type}
            )

        text = identifier.strip()
        if not text:
            return EntityLookupResult(found=False)

        # 1) 内码精确：纯数字标识视为 FID（LLM 可能直接给内码）
        if text.isdigit():
            by_id = await self._query(spec, f"{spec.id_field} = {text}", limit=1)
            if by_id:
                return _found(spec, by_id[0], fallback=text)

        # 2) 业务编号精确
        by_number = await self._query(spec, f"{spec.number_field} = {_quote_exact(text)}", limit=1)
        if by_number:
            return _found(spec, by_number[0], fallback=text)

        if not allow_name or spec.name_field == spec.number_field:
            return EntityLookupResult(found=False)

        # 3) 名称精确；命中多条 = 有歧义，交给用户消歧
        by_name = await self._query(
            spec, f"{spec.name_field} = {_quote_exact(text)}", limit=_AMBIGUITY_LIMIT
        )
        if len(by_name) == 1:
            return _found(spec, by_name[0], fallback=text)
        if len(by_name) > 1:
            return EntityLookupResult(
                found=False,
                candidates=tuple(_describe(spec, row) for row in by_name),
            )

        # 4) 名称模糊候选
        fuzzy = await self._query(
            spec,
            f"{spec.name_field} LIKE {_quote_fuzzy(text)}",
            limit=_SUGGESTION_LIMIT,
        )
        return EntityLookupResult(
            found=False,
            candidates=tuple(description for row in fuzzy if (description := _describe(spec, row))),
        )

    async def _query(
        self, spec: _EntitySpec, filter_string: str, *, limit: int
    ) -> list[dict[str, JsonScalar]]:
        """执行一次受限查询；字段集固定为 内码 + 编号 + 名称（去重）、保序。"""
        fields = [spec.id_field, spec.number_field]
        if spec.name_field not in fields:
            fields.append(spec.name_field)
        query = spec.endpoint.to_query_params(
            field_keys=",".join(fields), filter_string=filter_string, limit=limit
        )
        result = await self._client.execute_bill_query(query)
        return result.rows


def _quote_exact(value: str) -> str:
    """等值字面量；非法字符（单引号）归一为业务错误，避免裸 ValueError 外泄。"""
    try:
        return quote_eq(value)
    except ValueError as exc:
        raise KingdeeError("实体标识包含非法字符", context={"value_length": len(value)}) from exc


def _quote_fuzzy(value: str) -> str:
    """模糊字面量；同上。"""
    try:
        return quote_like(value)
    except ValueError as exc:
        raise KingdeeError("实体标识包含非法字符", context={"value_length": len(value)}) from exc


def _found(spec: _EntitySpec, row: dict[str, JsonScalar], *, fallback: str) -> EntityLookupResult:
    """命中行 → 结果；内码缺失时视为未命中（fail-closed）。"""
    canonical_id = _cell(row, spec.id_field)
    number = _cell(row, spec.number_field)
    display_name = _cell(row, spec.name_field) or number or canonical_id or fallback
    return EntityLookupResult(
        found=bool(canonical_id),
        canonical_id=canonical_id,
        display_name=display_name,
        number=number,
    )


def _describe(spec: _EntitySpec, row: dict[str, JsonScalar]) -> str:
    """候选描述：名称（编号），名称与编号相同时只留一个。"""
    name = _cell(row, spec.name_field)
    number = _cell(row, spec.number_field)
    if name and number and name != number:
        return f"{name}（{number}）"
    return name or number or _cell(row, spec.id_field)


def _cell(row: dict[str, JsonScalar], field: str) -> str:
    """单元格转字符串；None 视为无值。"""
    value = row.get(field)
    return "" if value is None else str(value).strip()
