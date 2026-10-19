"""Tool 定义框架（M2-T03）：Schema 声明 ↔ Python 实现绑定 → 注册表。

设计主张（方案 §3.2 三层解析法 + §4.1 黄金法则）：
- **LLM 永远不接触 FilterString 原文**——工具参数是结构化的，端点层组装；
  LLM 只能选值，不能拼 SQL。
- **Schema 即文档**：ToolSpec.parameters 直接由参数模型的
  ``model_json_schema()`` 生成——参数描述写在模型 Field 里，schema 零漂移。
- 工具层是薄壳：字段知识在端点层（kingdee/endpoints/），执行走 KingdeeClient，
  Guard 校验留给 M4（本层只做形状级 fail-closed 校验）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from agent.knowledge import KB_SEARCH_TOOL, KnowledgeParams, KnowledgeService
from config.settings import get_settings
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
from models.rag import KnowledgeSearchResult


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

# 本地工具执行器：接收已校验参数，返回结构化结果（M9-T09）
LocalRunner = Callable[[KingPilotBaseModel], Awaitable[Any]]

# 工具执行结果：金蝶查询或本地知识检索
ToolResult = BillQueryResult | KnowledgeSearchResult


@dataclass(frozen=True)
class ToolDefinition:
    """一个可执行工具：LLM 侧声明（ToolSpec）+ 参数模型 + 执行入口。

    两种形态：
    - **金蝶端点工具**：``endpoint`` + ``build_filter``，执行走 KingdeeClient
    - **本地工具**（M9-T09）：``runner``，执行不经金蝶，如 ``kb_search``

    ``tool_type`` / ``entity_references`` / ``required_parameters`` 是 M4 护栏的
    元数据：查询工具保持默认值，写/动作工具显式声明并由 Guard 3/5 校验。
    """

    spec: ToolSpec
    params_model: type[KingPilotBaseModel]
    build_filter: Callable[[KingPilotBaseModel], str] | None = None
    endpoint: EndpointDescriptor | None = None
    runner: LocalRunner | None = None
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

    @property
    def is_local(self) -> bool:
        """是否本地执行器（无金蝶 endpoint，不产生 FilterString）。"""
        return self.runner is not None

    async def execute(
        self,
        client: KingdeeClient,
        arguments: dict[str, Any],
        *,
        row_filter: str = "",
    ) -> ToolResult:
        """校验参数 → 本地执行或组装过滤条件查询。

        Args:
            row_filter: 可信行级过滤（M4 权限引擎产出）。在 Guard 校验之后追加，
                因此不参与 Guard 2 的文法校验；为空表示不注入。

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

        if self.runner is not None:
            if row_filter:
                raise ToolError(
                    f"本地工具不支持行级过滤: {self.spec.name}",
                    context={"tool": self.spec.name},
                )
            result = await self.runner(params)
            if not isinstance(result, (BillQueryResult, KnowledgeSearchResult)):
                raise ToolError(
                    f"本地工具返回了不支持的结果类型: {self.spec.name}",
                    context={"tool": self.spec.name},
                )
            return result

        if self.endpoint is None or self.build_filter is None:
            raise ToolError(
                f"工具缺少端点绑定或执行器: {self.spec.name}",
                context={"tool": self.spec.name},
            )

        try:
            filter_string = self.build_filter(params)
        except ValueError as exc:
            # 组装层拒绝（如单引号注入尝试）——归一为 ToolError，保持调用方单一接法
            raise ToolError(
                f"过滤条件被拒绝: {exc}",
                context={"tool": self.spec.name},
            ) from exc

        if row_filter:
            filter_string = f"{filter_string} AND {row_filter}" if filter_string else row_filter

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


_KNOWLEDGE_TOOL_DESCRIPTION = (
    "查询 KingPilot 内部知识库（操作手册、流程规范、制度说明）。"
    "仅用于“怎么做、流程是什么、有什么规定”类知识问题；"
    "不查询订单、库存、客户等实时 ERP 数据，也不用于写操作。"
)


def build_default_registry(*, knowledge: KnowledgeService | None = None) -> ToolRegistry:
    """装配默认注册表：5 个金蝶查询工具 + 1 个本地知识检索工具。

    ``knowledge`` 不传时按 Settings 的索引目录构造；索引不存在也注册，
    调用时返回「知识库没有找到相关文档」，不影响 ERP 查询。
    """
    registry = ToolRegistry()
    for binding in (
        SAL_ORDER_TOOL,
        PURCHASE_ORDER_TOOL,
        CUSTOMER_TOOL,
        SUPPLIER_TOOL,
        INVENTORY_TOOL,
    ):
        registry.register(_from_binding(binding))

    service = knowledge or KnowledgeService(index_dir=get_settings().rag_index_dir)
    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name=KB_SEARCH_TOOL,
                description=_KNOWLEDGE_TOOL_DESCRIPTION,
                parameters=KnowledgeParams.model_json_schema(),
            ),
            params_model=KnowledgeParams,
            runner=service.run,
        )
    )
    return registry
