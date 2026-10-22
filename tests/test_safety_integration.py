"""M4-T10：安全层接入 AgentCore 的端到端单测（FakeProvider/FakeClient 零网络）。

覆盖：越权拦截、HITL 取消、read_only 建议、工具列表过滤、行过滤注入。
"""

from datetime import datetime
from typing import Any, cast

from pydantic import Field

from agent.core import AgentCore
from agent.tools import (
    EntityReference,
    ToolDefinition,
    ToolRegistry,
    ToolType,
    build_default_registry,
)
from config.settings import Settings
from erp import BillQuerySpec, FilterExpression, eq
from kingdee.client import KingdeeClient
from kingdee.endpoints.base import EndpointDescriptor, EndpointParams, FieldDescriptor
from models.base import KingPilotBaseModel
from models.kingdee import BillQueryResult, JsonScalar
from models.llm import PlanResult, ToolCallDecision, ToolSpec
from models.user import UserContext
from safety.engine import SafetyLayer
from tests.fakes import FakeProvider


class FakeIO:
    """按顺序返回预设答案的确认接口。"""

    def __init__(self, answers: list[bool] | None = None) -> None:
        self._answers = list(answers or [])
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else False


class _FakeClient:
    """记录查询并固定返回一行数据的假客户端（T05 起收中性 BillQuerySpec）。"""

    def __init__(self) -> None:
        self.queries: list[BillQuerySpec] = []

    async def query(self, spec: BillQuerySpec) -> BillQueryResult:
        self.queries.append(spec)
        row: dict[str, JsonScalar] = {
            "FID": "100001",
            "FNumber": "CUS001",
            "FName": "客户A",
            "FBillNo": "SO1",
        }
        return BillQueryResult(
            form_id=spec.form_id,
            field_keys=list(spec.field_keys),
            rows=[row],
        )


class _WriteParams(EndpointParams):
    customer_number: str = Field(default="", description="客户内码")
    amount: float = Field(default=0.0, description="金额")


class _BillParams(EndpointParams):
    bill_no: str = Field(default="", description="单据编号")


def _empty_filter(params: KingPilotBaseModel) -> FilterExpression:
    return FilterExpression()


def _bill_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_BillParams, params)
    if not p.bill_no:
        return FilterExpression()
    return FilterExpression(conditions=(eq("FBillNo", p.bill_no),))


def _endpoint(form_id: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        form_id=form_id,
        label=form_id,
        fields=(
            FieldDescriptor(
                name="FBillNo", data_type="string", filterable=True, description="编号"
            ),
        ),
        default_field_keys=("FBillNo",),
        default_sort="FBillNo",
    )


def _tool(
    name: str,
    form_id: str,
    *,
    tool_type: ToolType = "query",
    action: str = "",
    params_model: type[EndpointParams] = _BillParams,
    build_filter: Any = _bill_filter,
    references: tuple[EntityReference, ...] = (),
    required: tuple[str, ...] = (),
) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name=name,
            description="测试工具",
            parameters=params_model.model_json_schema(),
        ),
        params_model=params_model,
        build_filter=build_filter,
        endpoint=_endpoint(form_id),
        tool_type=tool_type,
        action=action,
        entity_references=references,
        required_parameters=required,
    )


_CUSTOMER_REF = EntityReference(parameter="customer_number", entity_type="customer")

_WRITE_TOOL = _tool(
    "test_create_sales_order",
    "SAL_Order",
    tool_type="write",
    action="save",
    params_model=_WriteParams,
    build_filter=_empty_filter,
    references=(_CUSTOMER_REF,),
    required=("customer_number",),
)

_FINANCE_QUERY = _tool("erp_query_finance_vouchers", "GL_Voucher")

_FINANCE_WRITE = _tool(
    "erp_delete_finance_voucher",
    "GL_Voucher",
    tool_type="write",
    action="delete",
    params_model=_WriteParams,
    build_filter=_empty_filter,
    references=(_CUSTOMER_REF,),
    required=("customer_number",),
)


def _registry() -> ToolRegistry:
    registry = build_default_registry()
    for tool in (_WRITE_TOOL, _FINANCE_QUERY, _FINANCE_WRITE):
        registry.register(tool)
    return registry


def _settings() -> Settings:
    return Settings(_env_file=None, agent_max_steps=2)


def _plan(tool_name: str, arguments: dict[str, Any]) -> PlanResult:
    return PlanResult(tool_calls=[ToolCallDecision(tool_name=tool_name, arguments=arguments)])


def _core(
    provider: FakeProvider,
    *,
    io: FakeIO | None = None,
) -> tuple[AgentCore, _FakeClient]:
    registry = _registry()
    client = _FakeClient()
    client_for_core = cast(KingdeeClient, client)
    safety = SafetyLayer(
        registry,
        client_for_core,
        io or FakeIO(),
        clock=lambda: datetime(2026, 10, 19, 10, 0),
    )
    core = AgentCore(provider, registry, client_for_core, _settings(), safety=safety)
    return core, client


async def test_blocked_plan_returns_blocked_response() -> None:
    provider = FakeProvider(plan_queue=[_plan("erp_query_finance_vouchers", {"bill_no": "V1"})])
    core, client = _core(provider)

    response = await core.run("帮我查一下凭证", UserContext(user_id="u001", role="operator"))

    assert response.blocked is True
    assert "财务数据的访问权限" in response.content
    assert response.trace is not None
    assert response.trace.tool_executions == []
    assert response.trace.guard_checks
    assert response.trace.permission_check["allowed"] is False
    assert client.queries == []


async def test_hitl_cancel_returns_cancelled_response() -> None:
    provider = FakeProvider(
        plan_queue=[
            _plan(
                "test_create_sales_order",
                {"customer_number": "100001", "amount": 50_000},
            )
        ]
    )
    io = FakeIO([False])
    core, _ = _core(provider, io=io)

    response = await core.run("创建销售订单", UserContext(user_id="u001", role="salesperson"))

    assert response.cancelled is True
    assert response.blocked is False
    assert "取消" in response.content
    assert response.trace is not None
    assert response.trace.tool_executions == []
    assert len(io.prompts) == 1


async def test_read_only_plan_returns_advice_without_execution() -> None:
    provider = FakeProvider(
        plan_queue=[
            _plan(
                "erp_delete_finance_voucher",
                {"customer_number": "100001", "amount": 1_000_000},
            )
        ]
    )
    core, _ = _core(provider)

    response = await core.run("删除凭证", UserContext(user_id="u001", role="finance_staff"))

    assert response.blocked is False
    assert response.cancelled is False
    assert "人工执行" in response.content
    assert response.trace is not None
    assert response.trace.tool_executions == []


async def test_visible_tools_exclude_finance_for_operator() -> None:
    provider = FakeProvider(plan_queue=[PlanResult(tool_calls=[])])
    core, _ = _core(provider)

    await core.run("你好", UserContext(user_id="u001", role="operator"))

    names = [spec.name for spec in provider.last_plan_tools]
    assert "erp_query_finance_vouchers" not in names
    assert "erp_query_sales_order" in names


async def test_row_filter_is_injected_into_execution() -> None:
    provider = FakeProvider(plan_queue=[_plan("erp_query_sales_order", {"bill_no": "SO1"})])
    core, client = _core(provider)
    user = UserContext(user_id="u001", role="operator", data_scope={"org_ids": ["ORG001"]})

    response = await core.run("查订单", user)

    assert response.blocked is False
    assert response.trace is not None
    assert len(response.trace.tool_executions) == 1
    assert client.queries
    # LLM 侧条件在前，服务端可信行过滤按 AND 语义追加在后（D5）
    assert client.queries[-1].filter == FilterExpression(
        conditions=(eq("FBillNo", "SO1"), eq("FSaleOrgId.FNumber", "ORG001"))
    )
