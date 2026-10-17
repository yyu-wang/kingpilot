"""反思层（M3-T05，方案 §2.2 反思层 Reflect）。

职责：观察本步执行结果 → 判断「结果是否足够 / 是否继续」→ 归一化续跑指令。
主循环只调 ``reflect_step``，策略细节（何时值得问、指令如何防御）内聚在本模块。

设计要点：
- **没观察就不反思**：本步无工具执行（纯聊天）时没有可观察事实，
  调 LLM 判断「够不够」纯属浪费，直接跳过（``should_reflect``）。
- **观察回灌**：工具结果格式化为 user 消息追加进上下文，下一轮规划与
  本轮反思都能看见（两步任务「客户名称 → 编码」的关键）。
  tool 角色不带 tool_call_id 会被 OpenAI 兼容 API 拒绝，故映射为 user。
- **续跑指令防御**：LLM 说继续但给空指令 / 重复上轮指令时归一为停止，
  理由写进 reasoning——``agent_max_steps`` 之外再加一道语义防线。
"""

import json
from collections.abc import Sequence

from agent.llm import LLMProvider
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    ReflectionResult,
)
from models.tool import ToolExecution

# 观察消息前缀：与记忆层 tool→user 映射同款语义（agent/memory.py）
_OBSERVATION_PREFIX = "工具结果"


def build_execution_summary(executions: Sequence[ToolExecution]) -> ExecutionSummary:
    """按本步执行记录构造反思输入摘要。

    ``needs_more_steps`` 是「值得问模型是否继续」的启发式打标：有工具执行
    才可能多步（结果是否足够需要模型判断），无执行为纯聊天、无需续跑。
    """
    return ExecutionSummary(
        tool_executions=list(executions),
        needs_more_steps=bool(executions),
    )


def should_reflect(executions: Sequence[ToolExecution]) -> bool:
    """本步是否需要反思：有工具执行才有可观察结果可供判断。"""
    return bool(executions)


def observation_messages(executions: Sequence[ToolExecution]) -> list[LLMMessage]:
    """本步工具执行 → 观察消息（user 角色），供下一轮规划与反思。

    成功带上完整结构化结果（已由工具归一为 dict）；失败带上错误文本，
    让模型决定换参数重试或如实告知用户。
    """
    messages: list[LLMMessage] = []
    for execution in executions:
        if execution.error is None:
            payload = execution.result if execution.result is not None else {}
            messages.append(
                LLMMessage(
                    role="user",
                    content=f"{_OBSERVATION_PREFIX}({execution.tool_name}): "
                    f"{_render_payload(payload)}",
                )
            )
        else:
            messages.append(
                LLMMessage(
                    role="user",
                    content=f"工具 {execution.tool_name} 执行失败：{execution.error}",
                )
            )
    return messages


def normalize_reflection(
    result: ReflectionResult, *, previous_next_input: str = ""
) -> ReflectionResult:
    """续跑指令防御：空指令 / 重复上轮指令 → 归一为停止。

    Why 不在 Provider 层做：这是多步编排的循环安全策略，与协议解析无关；
    放在反思层可单测、可随编排策略演进（Provider 只负责出 JSON）。
    """
    if not result.continue_execution:
        return result
    next_input = result.next_input.strip()
    if not next_input:
        return _stop_with_reason(result, "续跑指令为空，已终止")
    if previous_next_input and next_input == previous_next_input.strip():
        return _stop_with_reason(result, "续跑指令与上轮重复，已终止以防死循环")
    if next_input != result.next_input:
        # 仅去首尾空白，保留原语义
        return result.model_copy(update={"next_input": next_input})
    return result


async def reflect_step(
    provider: LLMProvider,
    context: LLMContext,
    executions: Sequence[ToolExecution],
    memory: MemorySnapshot,
    *,
    previous_next_input: str = "",
) -> ReflectionResult:
    """调 Provider 反思本步结果，并做续跑指令归一化。

    Args:
        provider: LLM 协议实现（FakeProvider 或真 Provider）。
        context: 当前上下文（已含本步观察消息）。
        executions: 本步的工具执行记录（非空；空时调用方应跳过反思）。
        memory: 三层记忆快照（与 plan/generate 同一份）。
        previous_next_input: 上一轮反思产出的续跑指令（重复检测用）。

    Raises:
        LLMUnavailableError: LLM 不可用（降级规则引擎归 M6，本层传播）。
        LLMResponseError: 反思输出不是约定 JSON（fail-closed）。
    """
    summary = build_execution_summary(executions)
    result = await provider.reflect(context, summary, memory)
    return normalize_reflection(result, previous_next_input=previous_next_input)


def _stop_with_reason(result: ReflectionResult, reason: str) -> ReflectionResult:
    """保留模型原判断依据，追加终止原因，并清空续跑指令。"""
    combined = f"{result.reasoning}；{reason}" if result.reasoning else reason
    return ReflectionResult(continue_execution=False, next_input="", reasoning=combined)


def _render_payload(payload: object) -> str:
    """结构化结果 → 文本；非 JSON 可序列化对象直接 str（工具契约保证是 dict）。"""

    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)
