"""M7-T03：成本路由与用量记账单测。

覆盖四块：
1. 单价计算与 CostEngine 累计/聚合
2. ROUTING_CONFIG 档位解析与配置错误 fail-fast
3. RoutingProvider 按档位落到不同 model / base_url，set_tier 即时生效
4. get_provider 的自动路由与显式覆盖分流
"""

import json
from typing import Any, cast

import httpx
import pytest
import respx
from pydantic import SecretStr

from agent.providers import OpenAICompatibleProvider, get_provider
from config.model_registry import ModelRoute, ModelTier
from config.settings import Settings
from governance.cost_engine import (
    CostEngine,
    RoutingProvider,
    estimate_cost,
)
from infrastructure.exceptions import LLMError
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    TokenUsage,
)

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


def _settings(*, route: str = "chat_default", tier: ModelTier = "high") -> Settings:
    return Settings(
        _env_file=None,
        llm_route=route,
        llm_tier=tier,
        llm_api_key=SecretStr(_API_KEY),
    )


def _context() -> LLMContext:
    return LLMContext(messages=[LLMMessage(role="user", content="查订单")])


def _summary() -> ExecutionSummary:
    return ExecutionSummary(tool_executions=[], needs_more_steps=False)


def _memory() -> MemorySnapshot:
    return MemorySnapshot()


def _body(prompt: int = 100, completion: int = 20, content: str = "ok") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _provider(
    *,
    tier: ModelTier = "high",
    routes: dict[str, ModelRoute] | None = None,
    routing: dict[ModelTier, str] | None = None,
    sink: Any = None,
) -> RoutingProvider:
    return RoutingProvider(
        _settings(tier=tier),
        tier=tier,
        routes=routes or _ROUTES,
        routing=routing or _ROUTING,
        cost_engine=CostEngine(sink=sink),
    )


# ── 计价与记账 ──


def test_estimate_cost_uses_per_million_prices() -> None:
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=500)
    # (1000 × 2.0 + 500 × 4.0) / 1_000_000 = 0.004
    assert estimate_cost(_HIGH, usage) == 0.004


def test_cost_engine_records_fields() -> None:
    engine = CostEngine()

    record = engine.record(_HIGH, TokenUsage(prompt_tokens=100, completion_tokens=20))

    assert record.route_name == "test_high"
    assert record.tier == "high"
    assert record.model_id == "high-model"
    assert record.prompt_tokens == 100
    assert record.completion_tokens == 20
    assert record.cost_usd == pytest.approx(0.00028)
    assert record.recorded_at.tzinfo is not None


def test_cost_engine_accumulates_totals_and_summary() -> None:
    engine = CostEngine()
    engine.record(_HIGH, TokenUsage(prompt_tokens=100, completion_tokens=20))
    engine.record(_MID, TokenUsage(prompt_tokens=200, completion_tokens=40))
    engine.record(_ECONOMY, TokenUsage(prompt_tokens=300, completion_tokens=60))

    assert engine.total_tokens == 720
    assert engine.total_cost_usd == pytest.approx(0.00028 + 0.00028 + 0.0)

    summary = engine.summary()
    assert summary["high"]["calls"] == 1
    assert summary["mid"]["prompt_tokens"] == 200
    assert summary["economy"]["cost_usd"] == 0.0


def test_cost_engine_notifies_sink() -> None:
    captured: list[Any] = []
    engine = CostEngine(sink=captured.append)

    engine.record(_HIGH, TokenUsage(prompt_tokens=10, completion_tokens=5))

    assert len(captured) == 1
    assert captured[0].route_name == "test_high"


def test_cost_engine_isolates_bad_sink() -> None:
    """记账旁路异常不能打断 LLM 主链路。"""

    def broken(_: Any) -> None:
        raise RuntimeError("sink down")

    engine = CostEngine(sink=broken)

    record = engine.record(_HIGH, TokenUsage(prompt_tokens=10, completion_tokens=5))

    assert record.cost_usd > 0
    assert len(engine.records) == 1


# ── 配置校验 ──


def test_unknown_tier_fails_fast() -> None:
    with pytest.raises(LLMError, match="未知模型档位"):
        RoutingProvider(
            _settings(),
            tier=cast(ModelTier, "turbo"),
            routes=_ROUTES,
            routing=_ROUTING,
        )


def test_missing_tier_mapping_fails_fast() -> None:
    with pytest.raises(LLMError, match="缺少档位"):
        _provider(routing={"high": "high_route"})


def test_unknown_route_in_mapping_fails_fast() -> None:
    routing: dict[ModelTier, str] = {
        "high": "missing_route",
        "mid": "mid_route",
        "economy": "economy_route",
    }
    with pytest.raises(LLMError, match="指向未知路由"):
        _provider(routing=routing)


# ── 档位路由 ──


@respx.mock
async def test_high_tier_uses_high_model() -> None:
    route = respx.post("https://high.example/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    async with _provider(tier="high") as provider:
        await provider.generate_response(_context(), _summary(), _memory())

    payload = json.loads(route.calls[0].request.content)
    assert payload["model"] == "high-model"


@respx.mock
async def test_economy_tier_uses_local_endpoint() -> None:
    route = respx.post("http://economy.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    provider = _provider(tier="economy")
    try:
        await provider.generate_response(_context(), _summary(), _memory())
    finally:
        await provider.aclose()

    assert route.called
    assert json.loads(route.calls[0].request.content)["model"] == "economy-model"


@respx.mock
async def test_set_tier_switches_next_call() -> None:
    high_route = respx.post("https://high.example/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    economy_route = respx.post("http://economy.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    provider = _provider(tier="high")
    try:
        await provider.generate_response(_context(), _summary(), _memory())
        provider.set_tier("economy")
        await provider.generate_response(_context(), _summary(), _memory())
    finally:
        await provider.aclose()

    assert provider.current_tier == "economy"
    assert json.loads(high_route.calls[0].request.content)["model"] == "high-model"
    assert json.loads(economy_route.calls[0].request.content)["model"] == "economy-model"


@respx.mock
async def test_usage_is_recorded_with_tier_and_cost() -> None:
    respx.post("https://high.example/chat/completions").mock(
        return_value=httpx.Response(200, json=_body(prompt=100, completion=20))
    )
    provider = _provider(tier="high")
    try:
        await provider.generate_response(_context(), _summary(), _memory())
    finally:
        await provider.aclose()

    records = provider.cost_engine.records
    assert len(records) == 1
    assert records[0].tier == "high"
    assert records[0].model_id == "high-model"
    assert records[0].cost_usd == pytest.approx(0.00028)


@respx.mock
async def test_external_usage_sink_and_provider_reuse() -> None:
    respx.post("https://high.example/chat/completions").mock(
        return_value=httpx.Response(200, json=_body())
    )
    forwarded: list[TokenUsage] = []
    provider = RoutingProvider(
        _settings(tier="high"),
        routes=_ROUTES,
        routing=_ROUTING,
        usage_sink=forwarded.append,
    )
    try:
        # 两次同档位调用复用同一个底层 Provider，但每次都记账/转发
        await provider.generate_response(_context(), _summary(), _memory())
        await provider.generate_response(_context(), _summary(), _memory())
    finally:
        await provider.aclose()

    assert len(forwarded) == 2
    assert len(provider.cost_engine.records) == 2


@respx.mock
async def test_all_protocol_methods_delegate() -> None:
    respx.post("https://high.example/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json=_body()),
            httpx.Response(
                200,
                json=_body(content=json.dumps({"continue_execution": False})),
            ),
            httpx.Response(200, json=_body()),
            httpx.Response(
                200,
                json=_body(content=json.dumps({"summary_text": "旧对话摘要"})),
            ),
        ]
    )
    async with _provider(tier="high") as provider:
        assert (await provider.plan(_context(), [])).tool_calls == []
        reflection = await provider.reflect(_context(), _summary(), _memory())
        assert reflection.continue_execution is False
        assert await provider.generate_response(_context(), _summary(), _memory()) == "ok"
        draft = await provider.summarize([LLMMessage(role="user", content="旧对话")])
        assert draft.summary_text == "旧对话摘要"


# ── 工厂分流 ──


def test_get_provider_defaults_to_auto_routing() -> None:
    provider = get_provider(_settings(route="chat_default", tier="high"))
    assert isinstance(provider, RoutingProvider)
    assert provider.current_tier == "high"


def test_get_provider_explicit_route_overrides_tier() -> None:
    provider = get_provider(_settings(route="ollama_qwen", tier="high"))
    assert isinstance(provider, OpenAICompatibleProvider)
