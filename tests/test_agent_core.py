"""M3-T01：AgentCore 主循环骨架单测（FakeProvider + FakeTool，零网络）。

验收「FakeProvider + FakeTool 下主循环单测走通全 Phase」。
FakeTool（``_EchoTool``）覆盖 ``execute`` 返回固定 BillQueryResult——
不走真 ``ToolDefinition.execute``（那会经 build_filter 碰金蝶 client），
骨架测试只需证明「Phase 3 真执行了工具且结果进了 trace」。
"""

from typing import Any

from agent.core import AgentCore
from agent.tools import ToolDefinition, ToolRegistry
from config.settings import Settings
from kingdee.endpoints import SAL_ORDER
from kingdee.endpoints.base import EndpointParams
from models.kingdee import BillQueryResult
from models.llm import (
    PlanResult,
    ReflectionResult,
    TokenUsage,
    ToolCallDecision,
    ToolSpec,
)
from tests.fakes import FakeProvider

_ECHO_TOOL_NAME = "echo_test_tool"


class _EchoParams(EndpointParams):
    """空参数模型（limit 由 EndpointParams 提供）。"""


def _echo_build_filter(params: Any) -> str:
    return ""


class _EchoTool(ToolDefinition):
    """FakeTool：覆盖 execute 返回固定结果，不碰金蝶 client。"""

    async def execute(self, client: Any, arguments: dict[str, Any]) -> BillQueryResult:
        return BillQueryResult(form_id="ECHO", field_keys=["echo"], rows=[{"echo": "ok"}])


def _make_echo_registry() -> ToolRegistry:
    spec = ToolSpec(
        name=_ECHO_TOOL_NAME,
        description="测试回声工具：固定返回一行 {echo: ok}",
        parameters=_EchoParams.model_json_schema(),
    )
    registry = ToolRegistry()
    registry.register(
        _EchoTool(
            spec=spec,
            params_model=_EchoParams,
            build_filter=_echo_build_filter,
            endpoint=SAL_ORDER,
        )
    )
    return registry


def _settings(max_steps: int = 5) -> Settings:
    # _env_file=None：防本机 .env 的 KINGPILOT_* 污染（T03 教训：env_file 独立于 os.environ）
    return Settings(_env_file=None, agent_max_steps=max_steps)


def _plan(*tool_names: str) -> PlanResult:
    return PlanResult(
        tool_calls=[ToolCallDecision(tool_name=name, arguments={}) for name in tool_names]
    )


def _continue(next_input: str) -> ReflectionResult:
    return ReflectionResult(continue_execution=True, next_input=next_input)


def _stop() -> ReflectionResult:
    return ReflectionResult(continue_execution=False)


async def test_chat_single_turn_walks_all_phases() -> None:
    """纯聊天单轮：plan 空 → reflect 停 → 走通全 Phase。"""
    provider = FakeProvider(
        plan_queue=[_plan()], reflection_queue=[_stop()], response_queue=["你好"]
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("你好")

    assert response.content == "你好"
    assert provider.plan_calls == 1
    assert provider.reflect_calls == 1
    assert provider.generate_calls == 1
    trace = response.trace
    assert trace is not None
    assert trace.used_fallback is False
    assert trace.final_response == "你好"
    assert trace.total_latency_ms >= 0.0


async def test_single_tool_turn_executes_and_records() -> None:
    """单工具一轮：Phase 3 真执行 echo 工具，result 进 trace。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["查到了"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查一下")

    trace = response.trace
    assert trace is not None
    assert len(trace.tool_executions) == 1
    execution = trace.tool_executions[0]
    assert execution.tool_name == _ECHO_TOOL_NAME
    assert execution.error is None
    assert execution.result["rows"] == [{"echo": "ok"}]
    assert trace.llm_planning["tool_calls"][0]["tool_name"] == _ECHO_TOOL_NAME


async def test_two_step_orchestration_via_reflect() -> None:
    """两步编排：reflect 续跑一次 → plan_calls==2，next_input 追加进 context。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME), _plan()],
        reflection_queue=[_continue("再查它的订单"), _stop()],
        response_queue=["完成"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("先查客户")

    assert provider.plan_calls == 2
    assert provider.reflect_calls == 2
    trace = response.trace
    assert trace is not None
    assert len(trace.reflections) == 2
    assert trace.reflections[0]["continue_execution"] is True
    assert trace.reflections[0]["next_input"] == "再查它的订单"
    assert trace.used_fallback is False


async def test_max_steps_circuit_breaker_returns_partial() -> None:
    """熔断：reflect 永远续跑 → 循环在 max_steps 停，给部分结果不抛错。"""
    provider = FakeProvider(
        plan_queue=[_plan(), _plan(), _plan()],  # max_steps=3 → 3 轮
        reflection_queue=[_continue("继续"), _continue("继续"), _continue("继续")],
        response_queue=["部分结果"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings(max_steps=3))  # type: ignore[arg-type]

    response = await core.run("一直跑")

    assert provider.plan_calls == 3  # 恰好触顶
    trace = response.trace
    assert trace is not None
    assert trace.used_fallback is True  # 触顶停步标记
    assert response.content == "部分结果"  # 仍返回部分结果，不抛错


async def test_trace_threading_and_fields() -> None:
    """trace 贯穿：trace_id 一致、reflections 累积、耗时记录。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["ok"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查")

    trace = response.trace
    assert trace is not None
    assert response.trace_id == trace.trace_id
    assert trace.user_input == "查"
    assert trace.user_id == "local_dev"
    assert len(trace.reflections) == 1
    assert trace.total_latency_ms >= 0.0


async def test_token_usage_sink_channel() -> None:
    """规范 4：usage_sink 通道接通，每次 LLM 调用上报一次。"""
    captured: list[TokenUsage] = []
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=["ok"],
        usage_sink=captured.append,
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    await core.run("查")

    # plan + reflect + generate_response 各上报一次
    assert len(captured) == 3
    assert all(u.total_tokens > 0 for u in captured)
