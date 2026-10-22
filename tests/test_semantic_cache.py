"""M7-T04：只读计划语义缓存单测。

覆盖键归一化、TTL/LRU/深拷贝、只读判定，以及主循环命中缓存
（含「续跑计划不入缓存」与「写工具不缓存」）。
"""

from typing import Any, cast

from agent.core import AgentCore
from agent.tools import ToolDefinition, ToolRegistry, ToolType
from config.settings import Settings
from erp import FilterExpression
from governance.semantic_cache import (
    SemanticCache,
    build_cache_key,
    is_cacheable_plan,
)
from kingdee.client import KingdeeClient
from kingdee.endpoints.base import EndpointParams
from kingdee.endpoints.sal_order import SAL_ORDER
from models.kingdee import BillQueryResult
from models.llm import (
    PlanResult,
    ReflectionResult,
    ToolCallDecision,
    ToolSpec,
)
from tests.fakes import FakeProvider

_ECHO_TOOL_NAME = "cache_echo"


class _EchoParams(EndpointParams):
    """空参数模型。"""


class _EchoTool(ToolDefinition):
    """假工具：固定返回一行，不碰金蝶 client。"""

    async def execute(
        self, client: Any, arguments: dict[str, Any], *, row_filter: FilterExpression | None = None
    ) -> BillQueryResult:
        return BillQueryResult(form_id="ECHO", field_keys=["echo"], rows=[{"echo": "ok"}])


def _registry(tool_type: ToolType = "query") -> ToolRegistry:
    spec = ToolSpec(
        name=_ECHO_TOOL_NAME,
        description="缓存测试用的回声工具",
        parameters=_EchoParams.model_json_schema(),
    )
    registry = ToolRegistry()
    registry.register(
        _EchoTool(
            spec=spec,
            params_model=_EchoParams,
            build_filter=lambda _p: FilterExpression(),
            endpoint=SAL_ORDER,
            tool_type=tool_type,
            action="save" if tool_type == "write" else "",
        )
    )
    return registry


def _plan(*tool_names: str) -> PlanResult:
    return PlanResult(
        tool_calls=[ToolCallDecision(tool_name=name, arguments={}) for name in tool_names]
    )


def _settings() -> Settings:
    return Settings(_env_file=None, agent_max_steps=2)


def _key(text: str, *, tools: list[str] | None = None, role: str = "viewer") -> str:
    return build_cache_key(
        text,
        tool_names=tools or [_ECHO_TOOL_NAME],
        scope={"role": role, "data_scope": {}},
    )


# ── 键归一化 ──


def test_key_ignores_whitespace_and_punctuation() -> None:
    assert _key("查订单 SO001") == _key(" 查订单，SO001！ ")


def test_key_changes_with_entity() -> None:
    assert _key("查订单 SO001") != _key("查订单 SO002")


def test_key_changes_with_role_scope_or_tools() -> None:
    base = _key("查订单 SO001")
    assert base != _key("查订单 SO001", role="admin")
    assert base != build_cache_key(
        "查订单 SO001",
        tool_names=["other_tool"],
        scope={"role": "viewer", "data_scope": {}},
    )
    assert base != build_cache_key(
        "查订单 SO001",
        tool_names=[_ECHO_TOOL_NAME],
        scope={"role": "viewer", "data_scope": {"org_ids": ["ORG2"]}},
    )
    # 工具顺序不影响键
    assert _key("查订单 SO001", tools=["a", "b"]) == _key("查订单 SO001", tools=["b", "a"])


# ── TTL / LRU / 深拷贝 ──


def test_ttl_expires_entry() -> None:
    now = [0.0]
    cache = SemanticCache(ttl_seconds=10, max_entries=5, clock=lambda: now[0])
    cache.set("k", _plan(_ECHO_TOOL_NAME))

    now[0] = 11.0

    assert cache.get("k") is None
    assert cache.size == 0


def test_lru_evicts_oldest() -> None:
    cache = SemanticCache(ttl_seconds=100, max_entries=2, clock=lambda: 0.0)
    cache.set("a", _plan(_ECHO_TOOL_NAME))
    cache.set("b", _plan(_ECHO_TOOL_NAME))
    assert cache.get("a") is not None  # 命中后 a 变为最近使用

    cache.set("c", _plan(_ECHO_TOOL_NAME))

    assert cache.size == 2
    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None


def test_get_returns_deep_copy() -> None:
    cache = SemanticCache(clock=lambda: 0.0)
    cache.set("k", _plan(_ECHO_TOOL_NAME))

    first = cache.get("k")
    assert first is not None
    first.tool_calls.clear()

    second = cache.get("k")
    assert second is not None
    assert len(second.tool_calls) == 1


def test_clear_removes_all_entries() -> None:
    cache = SemanticCache(clock=lambda: 0.0)
    cache.set("a", _plan(_ECHO_TOOL_NAME))
    cache.set("b", _plan(_ECHO_TOOL_NAME))

    cache.clear()

    assert cache.size == 0
    assert cache.get("a") is None


def test_invalid_constructor_args() -> None:
    try:
        SemanticCache(ttl_seconds=0)
    except ValueError as exc:
        assert "TTL" in str(exc)
    else:
        raise AssertionError("ttl_seconds=0 应报错")

    try:
        SemanticCache(max_entries=0)
    except ValueError as exc:
        assert "容量" in str(exc)
    else:
        raise AssertionError("max_entries=0 应报错")


# ── 只读判定 ──


def test_is_cacheable_plan_requires_query_only() -> None:
    plan = _plan(_ECHO_TOOL_NAME)
    assert is_cacheable_plan(plan, {_ECHO_TOOL_NAME: "query"}) is True
    assert is_cacheable_plan(plan, {_ECHO_TOOL_NAME: "write"}) is False
    assert is_cacheable_plan(plan, {_ECHO_TOOL_NAME: "action"}) is False
    assert is_cacheable_plan(plan, {}) is False
    assert is_cacheable_plan(PlanResult(tool_calls=[]), {_ECHO_TOOL_NAME: "query"}) is False


# ── 主循环接入 ──


async def test_repeated_query_hits_cache() -> None:
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[
            ReflectionResult(continue_execution=False),
            ReflectionResult(continue_execution=False),
        ],
        response_queue=["第一次", "第二次"],
    )
    cache = SemanticCache()
    core = AgentCore(
        provider,
        _registry(),
        cast(KingdeeClient, None),
        _settings(),
        cache=cache,
    )

    await core.run("查一下测试数据")
    second = await core.run(" 查一下，测试数据！ ")

    assert provider.plan_calls == 1
    assert second.trace is not None
    assert second.trace.llm_planning["cache_hit"] is True
    assert cache.size == 1


async def test_continuation_plan_is_not_cached() -> None:
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME), _plan(_ECHO_TOOL_NAME)],
        reflection_queue=[
            ReflectionResult(continue_execution=True, next_input="再查一次"),
            ReflectionResult(continue_execution=False),
        ],
        response_queue=["完成"],
    )
    cache = SemanticCache()
    core = AgentCore(
        provider,
        _registry(),
        cast(KingdeeClient, None),
        _settings(),
        cache=cache,
    )

    await core.run("查一下测试数据")

    assert provider.plan_calls == 2
    # 只有首轮计划入缓存；reflect 续跑的第二轮计划不入缓存
    assert cache.size == 1


async def test_write_plan_is_not_cached() -> None:
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        response_queue=["完成"],
    )
    cache = SemanticCache()
    core = AgentCore(
        provider,
        _registry("write"),
        cast(KingdeeClient, None),
        _settings(),
        cache=cache,
    )

    await core.run("保存这张单据")

    assert cache.size == 0
