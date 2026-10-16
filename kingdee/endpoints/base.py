"""端点描述符基类（M1-T05）。

定位：**字段白名单的单一真相源**。M2（LLM 能问什么）、M4（Guard 2 校验什么）、
M1-T06b（脚本查什么）都从这里取数据——一处修改、多处生效。

Why 放 kingdee/endpoints/ 而非 config/：M1 模块规范 5 明确指定「endpoints/ 按 formId
组织端点描述，查询字段白名单在这里维护（为 M4 Guard 2 供数）」。config/ 的规则表
管的是策略开关，这里管的是协议元数据，两者职责不同。

Why @dataclass(frozen=True) 而非 Pydantic：规范 3.3 规定内部不可变值对象用 dataclass，
只有跨边界（API/配置/LLM JSON）的输入输出才用 Pydantic。白名单是内部元数据，
不需要每次访问付校验开销，且 frozen 提供更强的不可变约束。

本模块**只做声明，不做校验**：FilterString 的 AST 校验、字段存在性校验都是
M4 Guard 2 的职责（避免双重防线互相稀释）。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from models.base import KingPilotBaseModel
from models.kingdee import BillQueryParams

# 字段数据类型。直接决定 M2 提示词：date 的过滤值需带引号（'2026-01-01'），
# number 不带引号——类型信息缺失会让 LLM 生成语法错误的 FilterString。
FieldType = Literal["string", "number", "date", "enum", "related"]

# 与 BillQueryParams 的默认值保持一致（改这里要同步改模型）
_DEFAULT_LIMIT = 100
_DEFAULT_START_ROW = 0


@dataclass(frozen=True)
class FieldDescriptor:
    """单个查询字段的元数据。

    Why 区分 filterable：**可查 ≠ 可过滤**。关联字段 ``FCustomerID.FName`` 用于
    显示客户名称，但过滤必须换用编码形式 ``FCustomerID.FNumber``——按名称过滤
    既低效又不可靠（重名、改名）。M4 Guard 2 依据本标记校验 FilterString 的
    字段引用，这是「字段粒度的最小权限」。
    """

    name: str
    data_type: FieldType
    filterable: bool
    description: str  # 供 M2 LLM 理解字段语义（生成 FilterString 的依据）


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
        """可用于 FilterString 的字段名集合（由 fields 派生，不另写一份清单）。"""
        return frozenset(field.name for field in self.fields if field.filterable)

    @property
    def default_field_keys_csv(self) -> str:
        """默认查询字段的逗号分隔形式（金蝶 FieldKeys 参数格式）。"""
        return ",".join(self.default_field_keys)

    def to_query_params(
        self,
        *,
        field_keys: str | None = None,
        filter_string: str = "",
        order_string: str | None = None,
        start_row: int = _DEFAULT_START_ROW,
        limit: int = _DEFAULT_LIMIT,
    ) -> BillQueryParams:
        """按本端点的默认值组装查询请求；显式传参可覆盖默认。

        Why 收在描述符里：把「白名单 → 请求模型」这段接线集中一处，
        M1-T06b 脚本与 M2 工具层无需各自拼 FieldKeys 字符串。
        """
        return BillQueryParams(
            form_id=self.form_id,
            field_keys=field_keys if field_keys is not None else self.default_field_keys_csv,
            filter_string=filter_string,
            order_string=order_string if order_string is not None else self.default_sort,
            start_row=start_row,
            limit=limit,
        )


# ── M2-T03：查询工具的参数基类与 FilterString 组装设施 ──
# 设计主张：LLM 永远不接触 FilterString 原文——工具参数是结构化的
# （枚举/数值/短文本），由端点层组装。这是方案 §3.2 三层解析法的落地：
# LLM 只能选值，不能拼 SQL；单引号拒绝是纵深防御，M4 Guard 2 是主防线。


class EndpointParams(KingPilotBaseModel):
    """查询工具的结构化参数基类。

    ``limit`` 为所有查询工具共有（1-100 上限与沙箱/生产默认页宽对齐）；
    业务过滤参数由各端点子模型自行声明。``extra=forbid``（继承自
    KingPilotBaseModel）使 LLM 的未知参数在 ``model_validate`` 时直接拒绝。
    """

    limit: int = Field(default=100, ge=1, le=100, description="返回条数上限（1-100）")


# 单据状态值域（models.kingdee.DocumentStatus 的字符集，销售/采购订单共用）
_DOCUMENT_STATUS_VALUES = frozenset({"Z", "A", "B", "C", "D"})


def validate_document_status(value: str) -> str:
    """单据状态枚举校验（各订单端点参数模型共用的 field_validator 函数体）。

    空 = 不过滤；非空必须落在 Z/A/B/C/D 内（fail-closed，LLM 传 "X" 直接拒绝）。
    """
    if value and value not in _DOCUMENT_STATUS_VALUES:
        raise ValueError(f"单据状态须为 {'/'.join(sorted(_DOCUMENT_STATUS_VALUES))} 之一或留空")
    return value


def quote_eq(value: str) -> str:
    """等值条件的带引号字面量（``FDocumentStatus = 'C'``）。

    Why 拒绝而非转义单引号：金蝶 FilterString 的转义规则各版本不一（''/\\'），
    转义 = 把不确定的协议行为引入系统；拒绝把模糊性挡在边界外。
    """
    if "'" in value:
        raise ValueError(f"过滤值不能包含单引号: {value!r}")
    return f"'{value}'"


def quote_like(value: str) -> str:
    """模糊匹配条件的带引号字面量（``FName LIKE '%测试%'``）。单引号拒绝同上。"""
    if "'" in value:
        raise ValueError(f"过滤值不能包含单引号: {value!r}")
    return f"'%{value}%'"


def fmt_number(value: float) -> str:
    """数值条件的字面量：整数值去掉 ``.0`` 尾巴（金蝶 FilterString 数值比较惯例）。"""
    return str(int(value)) if value.is_integer() else repr(value)


@dataclass(frozen=True)
class QueryToolBinding:
    """端点 → 查询工具的绑定三件套（agent/tools.py 据此组装 ToolDefinition）。

    Why 放端点层而非 agent：字段知识（白名单/组装逻辑）属于端点自治，
    agent 只是组装者——这样 kingdee 包不反向依赖 agent，无循环导入。
    """

    tool_name: str
    tool_description: str
    endpoint: EndpointDescriptor
    params_model: type[EndpointParams]
    build_filter: Callable[[KingPilotBaseModel], str]
