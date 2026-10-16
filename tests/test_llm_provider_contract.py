"""LLMProvider 协议契约测试（M2-T01）。

核心价值：``run_provider_contract`` / ``run_unavailable_contract`` 是
可复用契约套件——T02 真实 Provider 的 integration 测试直接传入真实现
复跑同一组断言，保证「替身与真实现遵守同一契约」。
"""

import pytest

from agent.llm import LLMProvider
from infrastructure.exceptions import LLMResponseError, LLMUnavailableError
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    PlanResult,
    ReflectionResult,
    TokenUsage,
    ToolCallDecision,
    ToolSpec,
)
from tests.fakes import FakeProvider, unavailable_plan_provider

# ── 可复用契约套件（T02 真实现将复跑这两个函数）──


async def run_provider_contract(provider: LLMProvider) -> None:
    """正常路径契约：三方法签名与返回形状（对任意实现跑同一组断言）。"""
    context = LLMContext(messages=[LLMMessage(role="user", content="查最近 5 笔销售订单")])
    tools = [ToolSpec(name="kingdee_query_sales_order", description="查询销售订单")]

    plan = await provider.plan(context, tools)
    assert isinstance(plan, PlanResult)

    execution = ExecutionSummary()
    memory = MemorySnapshot()
    reflection = await provider.reflect(context, execution, memory)
    assert isinstance(reflection, ReflectionResult)
    assert isinstance(reflection.continue_execution, bool)

    response = await provider.generate_response(context, execution, memory)
    assert isinstance(response, str)
    assert response  # 非空——空答复对正常路径是契约违约


async def run_unavailable_contract(broken_provider: LLMProvider) -> None:
    """不可用契约：不可用时必须抛 LLMUnavailableError（主循环降级只 catch 它）。"""
    context = LLMContext(messages=[LLMMessage(role="user", content="任意输入")])
    with pytest.raises(LLMUnavailableError):
        await broken_provider.plan(context, tools=[])


# ── FakeProvider 跑契约套件（T01 验收标准）──


@pytest.mark.asyncio
async def test_contract_suite_on_fake_provider() -> None:
    """FakeProvider 通过协议契约套件。"""
    provider = FakeProvider(
        plan_queue=[
            PlanResult(
                tool_calls=[
                    ToolCallDecision(tool_name="kingdee_query_sales_order", arguments={"limit": 5})
                ]
            )
        ]
    )
    await run_provider_contract(provider)


@pytest.mark.asyncio
async def test_unavailable_contract_on_fake_provider() -> None:
    """FakeProvider 通过不可用契约（异常路径形状正确）。"""
    await run_unavailable_contract(unavailable_plan_provider())


@pytest.mark.asyncio
async def test_fake_provider_isinstance_protocol() -> None:
    """runtime_checkable 协议：FakeProvider 无需继承即被识别为 LLMProvider。"""
    assert isinstance(FakeProvider(), LLMProvider)


@pytest.mark.asyncio
async def test_fake_provider_queue_sequence_two_plans() -> None:
    """队列剧本：两次 plan 依次出队（多轮规划场景的时序表达）。"""
    first = PlanResult(tool_calls=[ToolCallDecision(tool_name="t1")])
    second = PlanResult(tool_calls=[ToolCallDecision(tool_name="t2")])
    provider = FakeProvider(plan_queue=[first, second])
    context = LLMContext()

    assert await provider.plan(context, tools=[]) is first
    assert await provider.plan(context, tools=[]) is second
    assert provider.plan_calls == 2


@pytest.mark.asyncio
async def test_fake_provider_queue_exhausted_fails_loud() -> None:
    """剧本耗尽抛 AssertionError——测试剧本不全应立刻暴露（fail-loud）。"""
    provider = FakeProvider(plan_queue=[PlanResult()])
    await provider.plan(LLMContext(), tools=[])
    with pytest.raises(AssertionError, match="plan_queue 已耗尽"):
        await provider.plan(LLMContext(), tools=[])


@pytest.mark.asyncio
async def test_fake_provider_exception_items_raise() -> None:
    """队列元素为异常实例时出队即抛（模拟超时/坏响应路径）。"""
    provider = FakeProvider(response_queue=[LLMResponseError("JSON 解析失败")])

    with pytest.raises(LLMResponseError):
        await provider.generate_response(LLMContext(), ExecutionSummary(), MemorySnapshot())


@pytest.mark.asyncio
async def test_fake_provider_usage_sink_reported_per_call() -> None:
    """模块规范 4 通道：每次 LLM 调用恰好上报一次 TokenUsage。"""
    reported: list[TokenUsage] = []

    def sink(usage: TokenUsage) -> None:
        reported.append(usage)

    provider = FakeProvider(plan_queue=[PlanResult(), PlanResult()], usage_sink=sink)
    await provider.plan(LLMContext(), tools=[])
    await provider.plan(LLMContext(), tools=[])

    assert len(reported) == 2
    assert all(usage.total_tokens == 120 for usage in reported)
