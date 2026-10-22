"""厂商中立的查询与过滤类型（ERP 适配层核心契约）。

Why 用 pydantic.dataclasses.dataclass 而非标准 dataclass：
``FilterExpression`` 会作为 ``BillQuerySpec`` 的字段跨边界（工具层 →
客户端适配器）传递，需要 Pydantic 序列化支持；同时保持 dataclass 的
轻量与 frozen 不可变约束，与项目「内部不可变值对象用 dataclass」的
既有约定（见 kingdee/endpoints/base.py 注释）一致。

Why FilterOp 用 StrEnum 而非 Literal：操作符集合是稳定闭集，StrEnum
自带 ``str()`` 与比较能力，且 Guard 2 做 ``in FilterOp`` 成员校验时
类型更明确。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from pydantic.dataclasses import dataclass

from models.base import KingPilotBaseModel

__all__ = [
    "BillQuerySpec",
    "FilterCondition",
    "FilterExpression",
    "FilterOp",
]


class FilterOp(StrEnum):
    """过滤操作符（厂商中立闭集）。各厂商渲染器负责翻译为各自方言。

    金蝶 FilterString 渲染为 ``= / <> / > / >= / < / <= / LIKE``；
    用友/SAP 渲染器按各自语法映射。
    """

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    LIKE = "like"


@dataclass(frozen=True)
class FilterCondition:
    """单个结构化过滤条件（AND 语义的组成单元）。

    ``field`` 必须命中端点的 ``filterable_fields``（Guard 2 校验）；
    ``value`` 的类型由端点字段的 ``data_type`` 决定：
    - ``string`` / ``enum`` / ``related`` / ``date`` → ``str``
      （date 字段收 ``"YYYY-MM-DD"`` 形式，渲染期才决定引号）
    - ``number`` → ``float``
    """

    field: str
    op: FilterOp
    value: str | float


@dataclass(frozen=True)
class FilterExpression:
    """过滤条件元组，语义 = AND 连接；空 = 无条件。

    无条件查询是否放行归 Guard 4 判定（与既有「空 FilterString 放行
    归 T05」的职责划分一致），本类型只承载形状。
    """

    conditions: tuple[FilterCondition, ...] = ()


class BillQuerySpec(KingPilotBaseModel):
    """中性查询请求（与厂商无关）。

    由各厂商适配器内部翻译为各自协议：
    - 金蝶 → ``models.kingdee.BillQueryParams`` + FilterString
    - 用友/SAP → 各自查询语法

    Why limit 上限 100：与工具层 ``EndpointParams.limit`` 的约束对齐
    （LLM 可见范围），金蝶语义上的 ``limit=0 不限`` 不在 LLM 可达面。
    """

    form_id: str = Field(min_length=1, description="业务对象标识（各厂商各自解释）")
    field_keys: tuple[str, ...] = Field(min_length=1, description="查询字段（关联字段用点号）")
    filter: FilterExpression = Field(default_factory=FilterExpression)
    order_by: str = Field(default="", description="排序字符串（如 'FDate DESC'）")
    start_row: int = Field(default=0, ge=0, description="行偏移，从 0 起")
    limit: int = Field(default=100, ge=1, le=100, description="返回条数上限（1-100）")
