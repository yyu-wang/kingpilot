"""AgentCore 主循环单测（M3-T01 骨架 + M3-T03 记忆接线，FakeProvider/FakeTool 零网络）。

验收「FakeProvider + FakeTool 下主循环单测走通全 Phase」。
FakeTool（``_EchoTool``）覆盖 ``execute`` 返回固定 BillQueryResult——
不走真 ``ToolDefinition.execute``（那会经 build_filter 碰金蝶 client），
骨架测试只需证明「Phase 3 真执行了工具且结果进了 trace」。
M3-T03 追加：记忆写回口径与 Phase 0 长历史压缩组装。
"""

from typing import Any

from agent.core import AgentCore
from agent.memory import MemoryStore
from agent.tools import ToolDefinition, ToolRegistry
from config.settings import Settings
from kingdee.endpoints import SAL_ORDER
from kingdee.endpoints.base import EndpointParams
from models.kingdee import BillQueryResult
from models.llm import (
    PlanResult,
    ReflectionResult,
    SummaryDraft,
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


# ── M3-T03：记忆接线 ──


async def test_memory_written_back_and_reflect_gets_snapshot() -> None:
    """有 memory：单轮写回 user/tool/assistant；reflect 收到真实（空）快照。"""
    memory = MemoryStore(session_id="sess-1")
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["最终答复"],
    )
    # echo tool 覆盖 execute，不碰 client（T01 既有模式）
    core = AgentCore(
        provider,
        _make_echo_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    response = await core.run("查一下")

    assert memory.raw_rounds == 1
    messages = memory.raw_messages()
    assert [m.role for m in messages] == ["user", "tool", "assistant"]
    assert messages[0].content == "查一下"
    assert messages[1].structured_result["rows"] == [{"echo": "ok"}]
    assert messages[2].content == "最终答复"
    # Phase 0 取快照在本轮写回之前——reflect 看到的 L1 为空（历史态）
    assert provider.last_reflect_memory is not None
    assert provider.last_reflect_memory.layer1_recent == []
    assert response.trace is not None
    assert response.trace.session_id == "sess-1"


async def test_phase0_compresses_long_history_before_planning() -> None:
    """预置 8 轮：Phase 0 先压缩再组装——plan 看到 system 摘要+6 轮历史+当前输入。"""
    memory = MemoryStore(session_id="sess-2")
    for i in range(1, 9):
        memory.add_raw("user", f"历史问题{i}")
        memory.add_raw("assistant", f"历史回复{i}")
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=["新答复"],
        summarize_queue=[SummaryDraft(summary_text="前两轮历史压缩摘要", key_entities=[])],
    )
    # echo tool 覆盖 execute，不碰 client（T01 既有模式）
    core = AgentCore(
        provider,
        _make_echo_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    await core.run("新问题")

    # Phase 0 压缩：8 轮压掉前 2 轮；Phase 6 写回本轮 → 7 轮
    assert provider.summarize_calls == 1
    assert memory.raw_rounds == 7
    context = provider.last_plan_context
    assert context is not None
    assert context.messages[0].role == "system"
    assert "前两轮历史压缩摘要" in context.messages[0].content
    assert context.messages[-1].content == "新问题"
    assembled = " ".join(m.content for m in context.messages)
    assert "历史问题1" not in assembled  # 被压缩轮的原文不进上下文
    assert "历史问题7" in assembled  # 保留段（相对第 6 轮）在
    # 本轮写回后最后两条为 user/assistant
    assert memory.raw_messages()[-1].content == "新答复"
