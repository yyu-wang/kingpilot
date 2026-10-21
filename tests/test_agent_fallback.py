"""M6-T02：主循环 LLM 降级联动单测。

覆盖 plan 不可用 → 规则引擎兜底、reflect/generate 不可用 → 确定性收尾、
写意图拒绝、无匹配提示、安全层工具过滤与结果渲染。
"""

from collections.abc import Collection
from typing import Any, cast

from pydantic import Field

from agent.core import AgentCore, render_fallback_response
from agent.rule_engine import FallbackPlan, RuleEngine
from agent.tools import ToolDefinition, ToolRegistry, build_default_registry
from config.settings import Settings
from infrastructure.exceptions import LLMUnavailableError
from kingdee.client import KingdeeClient
from kingdee.endpoints.base import EndpointDescriptor, EndpointParams, FieldDescriptor
from models.base import KingPilotBaseModel
from models.kingdee import BillQueryParams, BillQueryResult, JsonScalar
from models.llm import PlanResult, ReflectionResult, ToolCallDecision, ToolSpec
from models.tool import ToolExecution
from models.user import UserContext
from safety.engine import SafetyLayer
from tests.fakes import FakeProvider


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[BillQueryParams] = []

    async def execute_bill_query(self, params: BillQueryParams) -> BillQueryResult:
        self.queries.append(params)
        row: dict[str, JsonScalar] = {
            "FBillNo": "SO000008",
            "FDate": "2026-03-05",
            "FCustomerID.FName": "客户A",
            "FDocumentStatus": "C",
            "FBillAllAmount": 1000,
            "FMaterialId.FNumber": "MAT001",
            "FMaterialId.FName": "物料A",
            "FStockId.FName": "仓库A",
            "FBaseQty": "100",
        }
        return BillQueryResult(
            form_id=params.form_id,
            field_keys=params.field_keys.split(","),
            rows=[row],
        )


class FakeIO:
    async def ask(self, prompt: str) -> bool:
        return True


class _UnknownParams(EndpointParams):
    bill_no: str = Field(default="", description="单据编号")


def _unknown_filter(params: KingPilotBaseModel) -> str:
    return "FBillNo = 'X'"


_UNKNOWN_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_query_unknown",
        description="未知表单测试工具",
        parameters=_UnknownParams.model_json_schema(),
    ),
    params_model=_UnknownParams,
    build_filter=_unknown_filter,
    endpoint=EndpointDescriptor(
        form_id="UNKNOWN_FORM",
        label="未知表单",
        fields=(
            FieldDescriptor(
                name="FBillNo", data_type="string", filterable=True, description="编号"
            ),
        ),
        default_field_keys=("FBillNo",),
        default_sort="FBillNo",
    ),
)


class _StubRuleEngine(RuleEngine):
    """强制返回一个会被安全层拦截的规则计划。"""

    def match(
        self,
        user_input: str,
        *,
        available_tools: Collection[str] | None = None,
    ) -> FallbackPlan | None:
        return FallbackPlan(
            intent="stub",
            plan=PlanResult(
                tool_calls=[
                    ToolCallDecision(
                        tool_name="kingdee_query_unknown",
                        arguments={"bill_no": "X"},
                    )
                ]
            ),
        )


def _settings() -> Settings:
    return Settings(_env_file=None, agent_max_steps=3)


def _core(provider: FakeProvider, client: _FakeClient, **kwargs: Any) -> AgentCore:
    return AgentCore(
        provider,
        build_default_registry(),
        cast(KingdeeClient, client),
        _settings(),
        **kwargs,
    )


def _sales_plan() -> PlanResult:
    return PlanResult(
        tool_calls=[
            ToolCallDecision(
                tool_name="kingdee_query_sales_order",
                arguments={"bill_no": "SO000008"},
            )
        ]
    )


# ── plan 不可用 → 规则引擎 ──


async def test_plan_unavailable_falls_back_to_sales_query() -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])
    client = _FakeClient()

    response = await _core(provider, client).run("查销售订单 SO000008")

    assert "LLM 暂不可用" in response.content
    assert "SO000008" in response.content
    assert response.blocked is False
    assert response.trace is not None
    assert response.trace.used_fallback is True
    assert response.trace.llm_planning["fallback"] is True
    assert response.trace.llm_planning["intent"] == "sales_order"
    assert len(response.trace.tool_executions) == 1
    assert provider.generate_calls == 0
    assert len(client.queries) == 1


async def test_plan_unavailable_falls_back_to_inventory_query() -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])
    client = _FakeClient()

    response = await _core(provider, client).run("查物料 MAT001 的库存")

    assert "MAT001" in response.content
    assert response.trace is not None
    assert response.trace.llm_planning["intent"] == "inventory"


async def test_plan_unavailable_unmatched_returns_guidance() -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])

    response = await _core(provider, _FakeClient()).run("你好")

    assert "基础查询模式暂未理解" in response.content
    assert response.trace is not None
    assert response.trace.tool_executions == []
    assert response.trace.used_fallback is True


async def test_plan_unavailable_write_intent_is_refused() -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])

    response = await _core(provider, _FakeClient()).run("删除 SO000008")

    assert "写操作请稍后重试" in response.content
    assert response.trace is not None
    assert response.trace.tool_executions == []


# ── reflect / generate 不可用 ──


async def test_reflect_unavailable_renders_existing_results() -> None:
    provider = FakeProvider(
        plan_queue=[_sales_plan()],
        reflection_queue=[LLMUnavailableError("timeout")],
    )
    client = _FakeClient()

    response = await _core(provider, client).run("查销售订单 SO000008")

    assert "SO000008" in response.content
    assert response.trace is not None
    assert response.trace.used_fallback is True
    assert len(response.trace.tool_executions) == 1
    assert provider.generate_calls == 0


async def test_generate_unavailable_returns_friendly_message() -> None:
    provider = FakeProvider(
        plan_queue=[PlanResult(tool_calls=[])],
        response_queue=[LLMUnavailableError("timeout")],
    )

    response = await _core(provider, _FakeClient()).run("你好")

    assert "LLM 暂不可用" in response.content
    assert "基础查询模式" in response.content
    assert response.trace is not None
    assert response.trace.used_fallback is True


async def test_later_plan_failure_uses_existing_results() -> None:
    provider = FakeProvider(
        plan_queue=[_sales_plan(), LLMUnavailableError("timeout")],
        reflection_queue=[ReflectionResult(continue_execution=True, next_input="再查一次")],
    )
    client = _FakeClient()

    response = await _core(provider, client).run("查销售订单 SO000008")

    assert "已根据已完成的查询结果回答" in response.content
    assert "SO000008" in response.content
    assert response.trace is not None
    assert len(response.trace.tool_executions) == 1
    assert len(client.queries) == 1  # 不重复执行兜底查询


# ── 安全层工具过滤 ──


async def test_fallback_respects_visible_tools() -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])
    client = _FakeClient()
    registry = build_default_registry()
    safety = SafetyLayer(registry, cast(KingdeeClient, client), FakeIO())

    core = AgentCore(
        provider,
        registry,
        cast(KingdeeClient, client),
        _settings(),
        safety=safety,
    )
    response = await core.run("查物料 MAT001 的库存", UserContext(user_id="u001", role="viewer"))

    assert "权限不足" in response.content
    assert "库存数据的访问权限" in response.content
    assert response.blocked is True
    assert response.trace is not None
    assert response.trace.tool_executions == []
    assert response.trace.llm_planning["permission_denied"] is True


async def test_fallback_permission_denied_for_purchase_query() -> None:
    """viewer 查采购：直接说明权限不足，而不是笼统的「暂未理解」。"""
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])
    client = _FakeClient()
    registry = build_default_registry()
    safety = SafetyLayer(registry, cast(KingdeeClient, client), FakeIO())

    core = AgentCore(
        provider,
        registry,
        cast(KingdeeClient, client),
        _settings(),
        safety=safety,
    )
    response = await core.run(
        "查一下供应商 SUP001 的采购订单", UserContext(user_id="u001", role="viewer")
    )

    assert response.content.startswith("权限不足：")
    assert "采购数据的访问权限" in response.content
    assert "基础查询模式暂未理解" not in response.content
    assert response.blocked is True
    assert response.trace is not None
    assert response.trace.tool_executions == []


async def test_empty_plan_permission_denied_for_hidden_tool() -> None:
    """LLM 可见工具为空规划时，也要把「没权限」直接讲清楚。"""
    provider = FakeProvider(plan_queue=[PlanResult(tool_calls=[])])
    client = _FakeClient()
    registry = build_default_registry()
    safety = SafetyLayer(registry, cast(KingdeeClient, client), FakeIO())

    core = AgentCore(
        provider,
        registry,
        cast(KingdeeClient, client),
        _settings(),
        safety=safety,
    )
    response = await core.run("查一下采购订单 PO000002", UserContext(user_id="u001", role="viewer"))

    assert response.content.startswith("权限不足：")
    assert "采购数据的访问权限" in response.content
    assert response.blocked is True
    assert response.trace is not None
    assert response.trace.tool_executions == []
    assert response.trace.permission_check["denied_by"] == "permission_precheck"
    assert provider.generate_calls == 0


async def test_empty_plan_permission_denied_for_hidden_write_tool() -> None:
    """warehouse 发起采购订单写入：写工具不可见，不能编造「已创建」。"""
    provider = FakeProvider(plan_queue=[PlanResult(tool_calls=[])])
    client = _FakeClient()
    registry = build_default_registry()
    safety = SafetyLayer(registry, cast(KingdeeClient, client), FakeIO())

    core = AgentCore(
        provider,
        registry,
        cast(KingdeeClient, client),
        _settings(),
        safety=safety,
    )
    response = await core.run(
        "帮我创建一张采购订单：供应商 SUP001，物料 MAT001，数量 10，单价 100",
        UserContext(user_id="wanda", role="warehouse"),
    )

    assert response.content.startswith("权限不足：")
    assert "采购数据的访问权限" in response.content
    assert "已为您创建" not in response.content
    assert response.blocked is True
    assert response.trace is not None
    assert response.trace.tool_executions == []


async def test_fallback_write_intent_permission_denied_for_hidden_tool() -> None:
    """LLM 不可用时，warehouse 删除销售订单也要直接说明权限不足。"""
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])
    client = _FakeClient()
    registry = build_default_registry()
    safety = SafetyLayer(registry, cast(KingdeeClient, client), FakeIO())

    core = AgentCore(
        provider,
        registry,
        cast(KingdeeClient, client),
        _settings(),
        safety=safety,
    )
    response = await core.run(
        "删除销售订单 SO000004", UserContext(user_id="wanda", role="warehouse")
    )

    assert response.content.startswith("权限不足：")
    assert "销售数据的访问权限" in response.content
    assert response.blocked is True
    assert response.trace is not None
    assert response.trace.tool_executions == []


async def test_fallback_plan_blocked_by_safety_layer() -> None:
    """规则计划仍必须过安全审查；被拦时降级响应 blocked。"""
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])
    client = _FakeClient()
    registry = ToolRegistry()
    registry.register(_UNKNOWN_TOOL)
    safety = SafetyLayer(registry, cast(KingdeeClient, client), FakeIO())

    core = AgentCore(
        provider,
        registry,
        cast(KingdeeClient, client),
        _settings(),
        safety=safety,
        rule_engine=_StubRuleEngine(),
    )
    response = await core.run("查一下未知单据", UserContext(user_id="u001", role="operator"))

    assert response.blocked is True
    assert "未知表单" in response.content
    assert response.trace is not None
    assert response.trace.tool_executions == []


# ── 渲染器边界 ──


def test_render_fallback_response_handles_error_and_limit() -> None:
    rows = [{"FBillNo": f"SO{i:04d}"} for i in range(12)]
    executions = [
        ToolExecution(
            tool_name="kingdee_query_sales_order",
            parameters={},
            result={"field_keys": ["FBillNo"], "rows": rows},
        ),
        ToolExecution(tool_name="broken_tool", parameters={}, error="连接超时"),
    ]

    text = render_fallback_response(executions)

    assert "共 12 条" in text
    assert "仅显示前 10 条" in text
    assert "broken_tool 执行失败：连接超时" in text


def test_render_fallback_response_skips_non_dict_rows() -> None:
    executions = [
        ToolExecution(
            tool_name="kingdee_query_sales_order",
            parameters={},
            result={"rows": ["bad-row", {"FBillNo": "SO1"}]},
        )
    ]

    text = render_fallback_response(executions)

    assert "共 2 条" in text
    assert "FBillNo=SO1" in text


def test_render_fallback_response_empty() -> None:
    assert render_fallback_response([]) == "未执行任何查询。"
