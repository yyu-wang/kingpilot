"""M7-T04：预算熔断单测。

覆盖 BudgetPolicy 决策表、RoutingProvider 实际降档/拒绝、
预算事件的审计落盘与告警触发。
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from config.model_registry import ModelRoute, ModelTier
from config.settings import Settings
from governance.budget import BudgetPolicy
from governance.cost_engine import CostEngine, RoutingProvider
from infrastructure.exceptions import BudgetExceededError
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    TokenUsage,
)
from observability.alerts import AlertEngine
from observability.audit import AuditLogger, JsonlAuditSink

_API_KEY = "test-key-not-a-real-credential"  # pragma: allowlist secret

_HIGH = ModelRoute(
    name="test_high",
    provider_kind="openai_compatible",
    base_url="https://high.example",
    model_id="high-model",
    tier="high",
    input_cost_per_1m_usd=2.0,
    output_cost_per_1m_usd=4.0,
)
_MID = ModelRoute(
    name="test_mid",
    provider_kind="openai_compatible",
    base_url="https://mid.example",
    model_id="mid-model",
    tier="mid",
    input_cost_per_1m_usd=1.0,
    output_cost_per_1m_usd=2.0,
)
_ECONOMY = ModelRoute(
    name="test_economy",
    provider_kind="openai_compatible",
    base_url="http://economy.example/v1",
    model_id="economy-model",
    tier="economy",
)
_ROUTES = {"high_route": _HIGH, "mid_route": _MID, "economy_route": _ECONOMY}
_ROUTING: dict[ModelTier, str] = {
    "high": "high_route",
    "mid": "mid_route",
    "economy": "economy_route",
}


def _settings(tier: ModelTier = "high") -> Settings:
    return Settings(
        _env_file=None,
        llm_route="chat_default",
        llm_tier=tier,
        llm_api_key=SecretStr(_API_KEY),
    )


def _context() -> LLMContext:
    return LLMContext(messages=[LLMMessage(role="user", content="查订单")])


def _summary() -> ExecutionSummary:
    return ExecutionSummary(tool_executions=[], needs_more_steps=False)


def _memory() -> MemorySnapshot:
    return MemorySnapshot()


def _body() -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _engine_with_cost(route: ModelRoute, prompt_tokens: int) -> CostEngine:
    engine = CostEngine()
    engine.record(route, TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=0))
    return engine


# ── 决策表 ──


def test_policy_keeps_below_ratio() -> None:
    policy = BudgetPolicy(1.0, downgrade_ratio=0.8)
    decision = policy.evaluate(0.5, "high")
    assert decision.action == "keep"
    assert decision.tier == "high"
    assert policy.budget_usd == 1.0
    assert policy.downgrade_ratio == 0.8


def test_policy_downgrades_high_at_ratio() -> None:
    decision = BudgetPolicy(1.0, downgrade_ratio=0.8).evaluate(0.8, "high")
    assert decision.action == "downgrade"
    assert decision.tier == "mid"


def test_policy_downgrades_high_to_economy_at_budget() -> None:
    decision = BudgetPolicy(1.0, downgrade_ratio=0.8).evaluate(1.0, "high")
    assert decision.action == "downgrade"
    assert decision.tier == "economy"


def test_policy_downgrades_mid_at_budget() -> None:
    assert BudgetPolicy(1.0).evaluate(0.9, "mid").action == "keep"
    decision = BudgetPolicy(1.0).evaluate(1.0, "mid")
    assert decision.action == "downgrade"
    assert decision.tier == "economy"


def test_policy_exhausts_economy_at_budget() -> None:
    decision = BudgetPolicy(1.0).evaluate(1.0, "economy")
    assert decision.action == "exhausted"
    assert decision.tier == "economy"


def test_policy_keeps_economy_below_budget() -> None:
    decision = BudgetPolicy(1.0).evaluate(0.5, "economy")
    assert decision.action == "keep"
    assert decision.tier == "economy"


def test_policy_rejects_invalid_params() -> None:
    with pytest.raises(ValueError, match="预算"):
        BudgetPolicy(0)
    with pytest.raises(ValueError, match="降档比例"):
        BudgetPolicy(1.0, downgrade_ratio=1.0)


# ── RoutingProvider 集成 ──


@respx.mock
async def test_budget_downgrade_routes_to_mid() -> None:
    # 花费 0.006 >= 0.01 × 0.5 → high 降 mid
    engine = _engine_with_cost(_HIGH, prompt_tokens=3000)
    budget = BudgetPolicy(0.01, downgrade_ratio=0.5)
    route = respx.post("https://mid.example/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    provider = RoutingProvider(
        _settings("high"),
        routes=_ROUTES,
        routing=_ROUTING,
        cost_engine=engine,
        budget=budget,
    )
    async with provider:
        await provider.generate_response(_context(), _summary(), _memory())

    assert provider.current_tier == "mid"
    assert json.loads(route.calls[0].request.content)["model"] == "mid-model"


@respx.mock
async def test_budget_exhausted_blocks_call() -> None:
    engine = _engine_with_cost(_HIGH, prompt_tokens=1000)  # 0.002
    budget = BudgetPolicy(0.001)
    route = respx.post("http://economy.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    provider = RoutingProvider(
        _settings("economy"),
        routes=_ROUTES,
        routing=_ROUTING,
        cost_engine=engine,
        budget=budget,
    )
    async with provider:
        with pytest.raises(BudgetExceededError):
            await provider.generate_response(_context(), _summary(), _memory())

    assert route.called is False


@respx.mock
async def test_downgrade_emits_audit_and_alert(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(JsonlAuditSink(audit_path))
    alerts = AlertEngine()
    audit.subscribe(alerts.observe)

    engine = _engine_with_cost(_HIGH, prompt_tokens=3000)
    budget = BudgetPolicy(0.01, downgrade_ratio=0.5)
    respx.post("https://mid.example/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    provider = RoutingProvider(
        _settings("high"),
        routes=_ROUTES,
        routing=_ROUTING,
        cost_engine=engine,
        budget=budget,
        audit=audit,
    )
    async with provider:
        await provider.generate_response(_context(), _summary(), _memory())

    event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "budget.downgraded"
    assert event["detail"]["previous_tier"] == "high"
    assert event["detail"]["new_tier"] == "mid"

    fired = alerts.drain_alerts()
    assert len(fired) == 1
    assert fired[0].rule_id == "BUDGET_DOWNGRADED"
    assert fired[0].severity == "warning"


@respx.mock
async def test_exhausted_emits_audit_and_critical_alert(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(JsonlAuditSink(audit_path))
    alerts = AlertEngine()
    audit.subscribe(alerts.observe)

    engine = _engine_with_cost(_HIGH, prompt_tokens=1000)
    budget = BudgetPolicy(0.001)
    respx.post("http://economy.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    provider = RoutingProvider(
        _settings("economy"),
        routes=_ROUTES,
        routing=_ROUTING,
        cost_engine=engine,
        budget=budget,
        audit=audit,
    )
    async with provider:
        with pytest.raises(BudgetExceededError):
            await provider.generate_response(_context(), _summary(), _memory())

    event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["event_type"] == "budget.exhausted"
    assert event["status"] == "blocked"

    fired = alerts.drain_alerts()
    assert len(fired) == 1
    assert fired[0].rule_id == "BUDGET_EXHAUSTED"
    assert fired[0].severity == "critical"
