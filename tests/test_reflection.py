"""M3-T05：反思层单测（FakeProvider 零网络）。

覆盖：反思触发判定、执行摘要打标、观察消息格式、续跑指令防御归一化、
reflect_step 的 Provider 入参。
"""

from agent.reflection import (
    build_execution_summary,
    normalize_reflection,
    observation_messages,
    reflect_step,
    should_reflect,
)
from models.llm import LLMContext, LLMMessage, MemorySnapshot, ReflectionResult
from models.tool import ToolExecution
from tests.fakes import FakeProvider


def _execution(name: str, *, result: object = None, error: str | None = None) -> ToolExecution:
    return ToolExecution(tool_name=name, parameters={}, result=result, error=error)


# ── 触发判定与摘要 ──


def test_should_reflect_only_when_executions() -> None:
    """纯聊天（无工具执行）不反思；有执行才值得问「够不够」。"""
    assert should_reflect([]) is False
    assert should_reflect([_execution("t")]) is True


def test_build_execution_summary_flags_needs_more_steps() -> None:
    summary = build_execution_summary([_execution("a"), _execution("b")])
    assert summary.needs_more_steps is True
    assert [e.tool_name for e in summary.tool_executions] == ["a", "b"]
    assert build_execution_summary([]).needs_more_steps is False


# ── 观察消息 ──


def test_observation_messages_success_carries_structured_result() -> None:
    """成功结果以 user 角色回灌（tool 角色缺 tool_call_id 会被厂商 API 拒绝）。"""
    messages = observation_messages(
        [_execution("kingdee_query_customer", result={"rows": [{"FNumber": "CUS001"}]})]
    )
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert "kingdee_query_customer" in messages[0].content
    assert "CUS001" in messages[0].content
    assert messages[0].content.startswith("工具结果")


def test_observation_messages_error_carries_error_text() -> None:
    messages = observation_messages([_execution("t", error="连接超时")])
    assert len(messages) == 1
    assert "执行失败" in messages[0].content
    assert "连接超时" in messages[0].content


# ── 续跑指令归一化 ──


def test_normalize_passes_through_stop() -> None:
    result = ReflectionResult(continue_execution=False, reasoning="足够")
    assert normalize_reflection(result) is result


def test_normalize_blank_next_input_stops() -> None:
    """说继续但没给指令 → 归一为停止（无法执行空指令）。"""
    result = ReflectionResult(continue_execution=True, next_input="   ", reasoning="还要查")
    normalized = normalize_reflection(result)
    assert normalized.continue_execution is False
    assert normalized.next_input == ""
    assert "续跑指令为空" in normalized.reasoning
    assert "还要查" in normalized.reasoning  # 保留模型原判断依据


def test_normalize_duplicate_next_input_stops() -> None:
    """与上轮完全相同的续跑指令 → 停止（防语义死循环）。"""
    result = ReflectionResult(continue_execution=True, next_input="继续查")
    normalized = normalize_reflection(result, previous_next_input="继续查")
    assert normalized.continue_execution is False
    assert "重复" in normalized.reasoning


def test_normalize_strips_whitespace_but_keeps_continue() -> None:
    result = ReflectionResult(continue_execution=True, next_input="  查订单  ")
    normalized = normalize_reflection(result, previous_next_input="查客户")
    assert normalized.continue_execution is True
    assert normalized.next_input == "查订单"


# ── reflect_step ──


async def test_reflect_step_calls_provider_with_step_executions() -> None:
    """只把本步执行记录交给 Provider，needs_more_steps 按有执行打标。"""
    provider = FakeProvider(
        reflection_queue=[ReflectionResult(continue_execution=False, reasoning="足够")]
    )
    context = LLMContext(messages=[LLMMessage(role="user", content="查")])
    executions = [_execution("a"), _execution("b")]

    result = await reflect_step(
        provider, context, executions, MemorySnapshot(), previous_next_input=""
    )

    assert provider.reflect_calls == 1
    assert provider.last_reflect_summary is not None
    assert provider.last_reflect_summary.needs_more_steps is True
    assert len(provider.last_reflect_summary.tool_executions) == 2
    assert result.continue_execution is False
