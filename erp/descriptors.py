"""端点描述符基类（厂商中立，从 ``kingdee/endpoints/base.py`` 上收）。

定位：**字段白名单的单一真相源**。M2（LLM 能问什么）、M4（Guard 2 校验什么）、
M1-T06b（脚本查什么）都从这里取数据——一处修改、多处生效。

Why 放 erp/ 而非 kingdee/endpoints/：描述符本身是厂商中立概念（字段元数据 +
白名单派生），金蝶专属的只是 FilterString 渲染（在 ``kingdee/filter_string.py``）。
上收后 ``erp/descriptors.py`` 成为所有厂商共享的契约层。

Why @dataclass(frozen=True) 而非 Pydantic：规范 3.3 规定内部不可变值对象用
dataclass，只有跨边界（API/配置/LLM JSON）的输入输出才用 Pydantic。白名单是
内部元数据，不需要每次访问付校验开销，且 frozen 提供更强的不可变约束。

本模块**只做声明，不做校验**：FilterExpression 的字段白名单校验、值类型
匹配都是 Guard 2 的职责（避免双重防线互相稀释）。

T05 变更：``to_query_params`` 改产中性的 ``BillQuerySpec``（收
``FilterExpression``），不再产金蝶协议模型 ``BillQueryParams``——
厂商方言的翻译（FilterString 渲染）收进各厂商适配器内部。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from erp.types import BillQuerySpec, FilterExpression

# 字段数据类型。直接决定 M2 提示词：date 的过滤值需带引号（'2026-01-01'），
# number 不带引号——类型信息缺失会让 LLM 生成语法错误的过滤值。
FieldType = Literal["string", "number", "date", "enum", "related"]

# 与 BillQueryParams 的默认值保持一致（改这里要同步改模型）
_DEFAULT_LIMIT = 100
_DEFAULT_START_ROW = 0

__all__ = [
    "EndpointDescriptor",
    "FieldDescriptor",
    "FieldType",
]


@dataclass(frozen=True)
class FieldDescriptor:
    """单个查询字段的元数据。

    Why 区分 filterable：**可查 ≠ 可过滤**。关联字段 ``FCustomerID.FName``
    用于显示客户名称，但过滤必须换用编码形式 ``FCustomerID.FNumber``——
    按名称过滤既低效又不可靠（重名、改名）。M4 Guard 2 依据本标记校验
    过滤条件的字段引用，这是「字段粒度的最小权限」。
    """

    name: str
    data_type: FieldType
    filterable: bool
    description: str  # 供 M2 LLM 理解字段语义（生成过滤条件的依据）


@dataclass(frozen=True)
class EndpointDescriptor:
    """单据端点的查询契约（白名单的单一真相源）。"""

    form_id: str
    label: str
    fields: tuple[FieldDescriptor, ...]
    default_field_keys: tuple[str, ...]
    default_sort: str

    @property
    def selectable_fields(self) -> frozenset[str]:
        """可用于 FieldKeys 的字段名全集。"""
        return frozenset(field.name for field in self.fields)

    @property
    def filterable_fields(self) -> frozenset[str]:
        """可用于过滤条件的字段名集合（由 fields 派生，不另写一份清单）。"""
        return frozenset(field.name for field in self.fields if field.filterable)

    @property
    def default_field_keys_csv(self) -> str:
        """默认查询字段的逗号分隔形式（金蝶 FieldKeys 参数格式）。"""
        return ",".join(self.default_field_keys)

    def to_query_params(
        self,
        *,
        field_keys: tuple[str, ...] | None = None,
        filter: FilterExpression | None = None,
        order_by: str | None = None,
        start_row: int = _DEFAULT_START_ROW,
        limit: int = _DEFAULT_LIMIT,
    ) -> BillQuerySpec:
        """按本端点的默认值组装中性查询请求；显式传参可覆盖默认。

        Why 收在描述符里：把「白名单 → 请求模型」这段接线集中一处，
        M1-T06b 脚本与 M2 工具层无需各自拼字段清单。

        返回的 ``BillQuerySpec`` 是厂商中立契约；交给 ``ErpClient.query``
        后由各厂商适配器翻译为各自协议（金蝶 → FilterString）。
        """
        return BillQuerySpec(
            form_id=self.form_id,
            field_keys=field_keys if field_keys is not None else self.default_field_keys,
            filter=filter if filter is not None else FilterExpression(),
            order_by=order_by if order_by is not None else self.default_sort,
            start_row=start_row,
            limit=limit,
        )
