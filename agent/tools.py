"""Tool 定义框架（M2-T03）：Schema 声明 ↔ Python 实现绑定 → 注册表。

设计主张（方案 §3.2 三层解析法 + §4.1 黄金法则）：
- **LLM 永远不接触 FilterString 原文**——工具参数是结构化的，端点层组装；
  LLM 只能选值，不能拼 SQL。
- **Schema 即文档**：ToolSpec.parameters 直接由参数模型的
  ``model_json_schema()`` 生成——参数描述写在模型 Field 里，schema 零漂移。
- 工具层是薄壳：字段知识在端点层（kingdee/endpoints/），执行走 KingdeeClient，
  Guard 校验留给 M4（本层只做形状级 fail-closed 校验）。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from infrastructure.exceptions import ToolError
from kingdee.client import KingdeeClient
from kingdee.endpoints.base import EndpointDescriptor, EndpointParams, QueryToolBinding
from kingdee.endpoints.bd_customer import CUSTOMER_TOOL
from kingdee.endpoints.bd_supplier import SUPPLIER_TOOL
from kingdee.endpoints.pur_order import PURCHASE_ORDER_TOOL
from kingdee.endpoints.sal_order import SAL_ORDER_TOOL
from kingdee.endpoints.stk_inventory import INVENTORY_TOOL
from models.base import KingPilotBaseModel
from models.kingdee import BillQueryResult
from models.llm import ToolSpec


@dataclass(frozen=True)
class EntityReference:
    """写操作参数 → 业务实体的引用声明（M4-T04 Guard 3 依据）。

    ``parameter`` 是工具参数名（如 ``customer_number``），``entity_type`` 是
    待预检的实体类型（customer/supplier/order）。Guard 3 只看声明、不猜参数名——
    新增写工具必须显式声明，否则 fail-closed 拦截。
    """

    parameter: str
    entity_type: str
    allow_name: bool = True  # 是否允许传名称（预检负责换成系统编码）


# 工具类型：服务端事实，不信任 LLM 自述（提示注入可把写操作标成查询）
ToolType = Literal["query", "write", "action"]


@dataclass(frozen=True)
class ToolDefinition:
    """一个可执行工具：LLM 侧声明（ToolSpec）+ 参数模型 + 组装器 + 端点绑定。

    ``execute`` 是唯一执行入口：Pydantic 校验 LLM 给的 arguments（fail-closed，
    未知参数/越界/非法枚举全拒）→ 端点组装 FilterString → KingdeeClient 查询。
    返回 ``BillQueryResult``（rows 已按 FieldKeys 转成 dict，可直接序列化进对话）。

    ``tool_type`` / ``entity_references`` / ``required_parameters`` 是 M4 护栏的
    元数据：查询工具保持默认值，写/动作工具显式声明并由 Guard 3/5 校验。
    """

    spec: ToolSpec
    params_model: type[EndpointParams]
    build_filter: Callable[[KingPilotBaseModel], str]
    endpoint: EndpointDescriptor
    tool_type: ToolType = "query"
    entity_references: tuple[EntityReference, ...] = ()
    # 业务必填项：可多于 Schema 的 required（如 Schema 允许空串、业务不允许）
    required_parameters: tuple[str, ...] = ()
    # 写操作动作（save/submit/audit/unaudit/delete），风险规则按它匹配
    action: str = ""

    @property
    def name(self) -> str:
        """工具名（LLM tool_calls 里的对齐键）。"""
        return self.spec.name

    async def execute(self, client: KingdeeClient, arguments: dict[str, Any]) -> BillQueryResult:
        """校验参数 → 组装过滤条件 → 查询。

        Raises:
            ToolError: 参数校验失败（含未知参数）、过滤值被组装层拒绝；
                原始 ValidationError/ValueError 挂在 ``__cause__`` 供调试。
        """
        try:
            params = self.params_model.model_validate(arguments)
        except ValidationError as exc:
            detail = "; ".join(
                "{}: {}".format(".".join(str(part) for part in error["loc"]), error["msg"])
                for error in exc.errors()[:3]
            )
            raise ToolError(
                f"工具参数校验失败: {self.spec.name}（{detail}）",
                context={"tool": self.spec.name},
            ) from exc

        try:
            filter_string = self.build_filter(params)
        except ValueError as exc:
            # 组装层拒绝（如单引号注入尝试）——归一为 ToolError，保持调用方单一接法
            raise ToolError(
                f"过滤条件被拒绝: {exc}",
                context={"tool": self.spec.name},
            ) from exc

        # EndpointParams 保证 limit 存在；isinstance 收窄供 mypy 取属性
        if not isinstance(params, EndpointParams):
            raise ToolError(f"参数模型 {type(params).__name__} 未继承 EndpointParams")
        query = self.endpoint.to_query_params(filter_string=filter_string, limit=params.limit)
        return await client.execute_bill_query(query)


class ToolRegistry:
    """工具注册表：按名注册/取用/导出全部 LLM 声明。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        """注册工具；同名重复注册直接拒绝（装配期错误尽早暴露）。"""
        if tool.name in self._tools:
            raise ToolError(f"工具重复注册: {tool.name}", context={"tool": tool.name})
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        """按名取工具；未知名抛 ToolError 并携带已注册名单（LLM 幻觉工具名的纠错线索）。"""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(
                f"未知工具: {name}", context={"tool": name, "known": sorted(self._tools)}
            ) from exc

    def all_specs(self) -> list[ToolSpec]:
        """全部工具的 LLM 侧声明（Phase 1 组装 ``tools`` 参数用，顺序 = 注册序）。"""
        return [tool.spec for tool in self._tools.values()]

    def all_tools(self) -> list[ToolDefinition]:
        """全部工具定义（M4 权限过滤等需要 form_id/tool_type 的场景用）。"""
        return list(self._tools.values())


def _from_binding(binding: QueryToolBinding) -> ToolDefinition:
    """端点绑定三件套 → ToolDefinition（schema 由参数模型自动生成）。"""
    spec = ToolSpec(
        name=binding.tool_name,
        description=binding.tool_description,
        parameters=binding.params_model.model_json_schema(),
    )
    return ToolDefinition(
        spec=spec,
        params_model=binding.params_model,
        build_filter=binding.build_filter,
        endpoint=binding.endpoint,
    )


def build_default_registry() -> ToolRegistry:
    """装配默认注册表：5 个查询工具（销售/采购订单、客户、供应商、库存）。"""
    registry = ToolRegistry()
    for binding in (
        SAL_ORDER_TOOL,
        PURCHASE_ORDER_TOOL,
        CUSTOMER_TOOL,
        SUPPLIER_TOOL,
        INVENTORY_TOOL,
    ):
        registry.register(_from_binding(binding))
    return registry
