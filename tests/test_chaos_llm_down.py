"""M6-T07：混沌验收——LLM 全不可用时只读仍可用。

每个用例都用「plan/reflect/generate 全部不可用」的 Provider，
验证规则引擎兜底 + M4 安全管道 + M3 执行链路能独立完成只读查询；
写意图必须拒绝，绝不降级执行。
"""

from typing import cast

import pytest

from agent.core import AgentCore
from agent.tools import build_default_registry
from config.settings import Settings
from infrastructure.exceptions import LLMUnavailableError
from kingdee.client import KingdeeClient
from models.kingdee import BillQueryParams, BillQueryResult, JsonScalar
from models.user import UserContext
from safety.engine import SafetyLayer
from tests.fakes import FakeProvider

# (输入, 预期工具, 预期参数子集, 预期 intent)
CASES: tuple[tuple[str, str, dict[str, str], str], ...] = (
    (
        "查订单 SO000008 的交货日期",
        "kingdee_query_sales_order",
        {"bill_no": "SO000008"},
        "sales_order",
    ),
    (
        "查询销售订单 CUS001 的明细",
        "kingdee_query_sales_order",
        {"customer_number": "CUS001"},
        "sales_order",
    ),
    (
        "查已审核的销售订单",
        "kingdee_query_sales_order",
        {"document_status": "C"},
        "sales_order",
    ),
    (
        "查未审核的销售订单",
        "kingdee_query_sales_order",
        {"document_status": "A"},
        "sales_order",
    ),
    (
        "查订单 SO000009 的金额",
        "kingdee_query_sales_order",
        {"bill_no": "SO000009"},
        "sales_order",
    ),
    (
        "查采购订单 SUP001 的到货日期",
        "kingdee_query_purchase_order",
        {"supplier_number": "SUP001"},
        "purchase_order",
    ),
    (
        "查已审核的采购订单",
        "kingdee_query_purchase_order",
        {"document_status": "C"},
        "purchase_order",
    ),
    (
        "查未审核的采购订单",
        "kingdee_query_purchase_order",
        {"document_status": "A"},
        "purchase_order",
    ),
    (
        "查物料 MAT001 的库存",
        "kingdee_query_inventory",
        {"material_number": "MAT001"},
        "inventory",
    ),
    (
        "查库存 MAT002",
        "kingdee_query_inventory",
        {"material_number": "MAT002"},
        "inventory",
    ),
    (
        "查客户 深圳XX科技有限公司",
        "kingdee_query_customer",
        {"name": "深圳XX科技有限公司"},
        "customer",
    ),
    (
        "查供应商 深圳YY贸易",
        "kingdee_query_supplier",
        {"name": "深圳YY贸易"},
        "supplier",
    ),
)


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[BillQueryParams] = []

    async def execute_bill_query(self, params: BillQueryParams) -> BillQueryResult:
        self.queries.append(params)
        row: dict[str, JsonScalar] = {
            "FBillNo": "SO000008",
            "FName": "深圳XX科技有限公司",
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


class _FakeIO:
    async def ask(self, prompt: str) -> bool:
        return True


class _DownProvider(FakeProvider):
    """所有 LLM 阶段都不可用。"""

    def __init__(self) -> None:
        super().__init__(
            plan_queue=[LLMUnavailableError("plan down")],
            reflection_queue=[LLMUnavailableError("reflect down")],
            response_queue=[LLMUnavailableError("generate down")],
        )


def _settings() -> Settings:
    return Settings(_env_file=None, agent_max_steps=3)


def _core(client: _FakeClient) -> tuple[AgentCore, _DownProvider]:
    registry = build_default_registry()
    provider = _DownProvider()
    safety = SafetyLayer(registry, cast(KingdeeClient, client), _FakeIO())
    core = AgentCore(
        provider,
        registry,
        cast(KingdeeClient, client),
        _settings(),
        safety=safety,
    )
    return core, provider


def _manager() -> UserContext:
    return UserContext(user_id="u001", role="manager")


@pytest.mark.parametrize(("user_input", "tool_name", "arguments", "intent"), CASES)
async def test_readonly_query_survives_llm_outage(
    user_input: str,
    tool_name: str,
    arguments: dict[str, str],
    intent: str,
) -> None:
    client = _FakeClient()
    core, provider = _core(client)

    response = await core.run(user_input, _manager())

    assert response.blocked is False
    assert response.cancelled is False
    assert "基础查询模式" in response.content
    assert "共 1 条" in response.content
    assert response.trace is not None
    assert response.trace.used_fallback is True
    assert response.trace.llm_planning["fallback"] is True
    assert response.trace.llm_planning["intent"] == intent

    assert len(response.trace.tool_executions) == 1
    execution = response.trace.tool_executions[0]
    assert execution.tool_name == tool_name
    for key, value in arguments.items():
        assert execution.parameters[key] == value

    # LLM 只在最初尝试过一次，之后完全绕过
    assert provider.plan_calls == 1
    assert provider.reflect_calls == 0
    assert provider.generate_calls == 0
    assert len(client.queries) == 1


def test_chaos_set_has_enough_cases() -> None:
    assert len(CASES) >= 10


@pytest.mark.parametrize(
    "user_input",
    ["删除订单 SO000008", "把 SO000008 的金额改成 100"],
)
async def test_write_intents_are_refused_when_llm_down(user_input: str) -> None:
    client = _FakeClient()
    core, provider = _core(client)

    response = await core.run(user_input, _manager())

    assert response.blocked is False
    assert "基础查询模式暂未理解" in response.content
    assert response.trace is not None
    assert response.trace.used_fallback is True
    assert response.trace.tool_executions == []
    assert client.queries == []
    assert provider.plan_calls == 1
