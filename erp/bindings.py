"""端点工具绑定（厂商中立，从 ``kingdee/endpoints/base.py`` 上收）。

设计主张（方案 §3.2 三层解析法 + §4.1 黄金法则）：
- **LLM 永远不接触查询方言原文**——工具参数是结构化的
  （枚举/数值/短文本），由端点层组装为 ``FilterExpression``；
  LLM 只能选值，不能拼 SQL。单引号拒绝是纵深防御，M4 Guard 2 是主防线。
- ``build_filter`` 返回中性的 ``FilterExpression``（T05 起）；
  厂商方言的翻译（如金蝶 FilterString 渲染）收进各厂商适配器内部，
  工具层与 Guard 层只见结构化表达式。

Why ``WriteRemoteRunner`` 用 ``ErpClient`` 而非 ``KingdeeClient``：
``erp/`` 是厂商中立契约包，反向依赖 ``kingdee/`` 会造成循环
（``kingdee/`` 已实现 ``ErpClient`` Protocol）。``KingdeeClient`` 满足
Protocol 后，写工具 runner 的形参与体内调用同步从金蝶 API 切换到
中性 API（``invoke_action``），不依赖具体厂商。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from erp.descriptors import EndpointDescriptor
from erp.protocol import ErpClient
from erp.types import FilterExpression
from models.base import KingPilotBaseModel
from models.kingdee import ActionResult

__all__ = [
    "EndpointParams",
    "EntityReference",
    "QueryToolBinding",
    "WriteRemoteRunner",
    "WriteToolBinding",
    "validate_document_status",
]


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


@dataclass(frozen=True)
class QueryToolBinding:
    """端点 → 查询工具的绑定三件套（agent/tools.py 据此组装 ToolDefinition）。

    Why 放端点层而非 agent：字段知识（白名单/组装逻辑）属于端点自治，
    agent 只是组装者——这样 erp 包不反向依赖 agent，无循环导入。

    ``build_filter`` 返回中性 ``FilterExpression``（空 = 无条件）；
    ``build_field_keys`` 返回字段元组，None = 用端点默认字段。
    """

    tool_name: str
    tool_description: str
    endpoint: EndpointDescriptor
    params_model: type[EndpointParams]
    build_filter: Callable[[KingPilotBaseModel], FilterExpression]
    # 可选：按参数决定 FieldKeys（如「是否展开明细行」）；None = 用端点默认字段
    build_field_keys: Callable[[KingPilotBaseModel], tuple[str, ...] | None] | None = None


@dataclass(frozen=True)
class EntityReference:
    """写操作参数 → 业务实体的引用声明（Guard 3 依据）。

    ``parameter`` 是工具参数名（如 ``customer_number``），``entity_type`` 是
    待预检的实体类型（customer/supplier/material/order）。Guard 3 只看声明、
    不猜参数名——新增写工具必须显式声明，否则 fail-closed 拦截。
    """

    parameter: str
    entity_type: str
    allow_name: bool = True  # 是否允许传名称（预检负责换成系统编码）


# 远程写执行器：接收已校验参数，经 ErpClient.invoke_action 调用厂商写接口
WriteRemoteRunner = Callable[[ErpClient, KingPilotBaseModel], Awaitable[ActionResult]]


@dataclass(frozen=True)
class WriteToolBinding:
    """端点 → 写工具的绑定（agent/tools.py 据此组装 ToolDefinition）。

    ``tool_type`` 区分创建（write，必须声明实体引用）与状态流转（action，
    单号由 Guard 5 的业务必填项兜底）。
    """

    tool_name: str
    tool_description: str
    endpoint: EndpointDescriptor
    params_model: type[KingPilotBaseModel]
    action: str
    runner: WriteRemoteRunner
    tool_type: Literal["write", "action"] = "action"
    entity_references: tuple[EntityReference, ...] = ()
    required_parameters: tuple[str, ...] = ()
