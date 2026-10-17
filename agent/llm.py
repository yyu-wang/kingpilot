"""LLMProvider 协议（M2-T01）。

方案 §2.3 主循环对 LLM 的全部依赖收敛为协议方法（忠实伪代码签名）：

- ``plan``：Phase 1 规划——给定上下文与可见工具，产出待执行调用序列
- ``reflect``：Phase 4 反思——判断结果是否足够、是否继续多步编排
- ``generate_response``：Phase 4 出稿——基于执行结果生成自然语言答复
- ``summarize``：记忆压缩（M3-T03）——把旧对话压缩为结构化摘要草稿

Why ``typing.Protocol`` 而非 ABC：面向协议编程（模块规范 1）只要求
「有这些方法且签名一致」，不要求继承——具体厂商实现（T02）与测试
替身（tests/fakes.py）无需 ``class FakeProvider(LLMProvider)`` 即被
mypy 校验为满足协议；继承关系反而把「厂商代码」耦合进「项目代码」。

Usage：token 用量经 ``TokenUsageSink`` 构造注入上报（D3 决策）——
协议方法返回值保持与伪代码一致（纯业务对象），用量通道不污染签名；
M7 成本核算从 sink 接线到 trace。
"""

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    PlanResult,
    ReflectionResult,
    SummaryDraft,
    TokenUsage,
    ToolSpec,
)

# 模块规范 4 的用量通道：Provider 每次真实调用 LLM 后回调一次
TokenUsageSink = Callable[[TokenUsage], None]


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 供应商协议：plan / reflect / generate_response / summarize。"""

    async def plan(self, context: LLMContext, tools: Sequence[ToolSpec]) -> PlanResult:
        """Phase 1 规划：决定调用哪些工具、传什么参数。

        Args:
            context: 已组装的 LLM 上下文（含系统提示、历史、防幻觉锚点）。
            tools: 数据域过滤后的可见工具声明（无权限的工具 LLM 看不到）。

        Returns:
            规划结果；``tool_calls`` 为空表示纯聊天无需工具。

        Raises:
            LLMUnavailableError: 超时/网络/服务端 5xx（主循环据此触发降级）。
            LLMRateLimitError: 供应商限流（failover 链触发条件之一）。
            LLMResponseError: 响应不合法（JSON 解析失败 / tool call 参数不符 Schema）。
        """
        ...

    async def reflect(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> ReflectionResult:
        """Phase 4 反思：执行结果是否足以回答用户。

        Args:
            context: LLM 上下文快照。
            execution: 本轮工具执行摘要（含 needs_more_steps 信号源）。
            memory: 三层记忆快照（多指代解析依赖近期对话）。

        Returns:
            续跑决策（continue_execution + next_input）。
        """
        ...

    async def generate_response(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> str:
        """Phase 4 出稿：基于执行结果生成面向用户的自然语言答复。

        产出直接进 Phase 5 幻觉校验——禁止编造执行摘要之外的数字/单据号。
        """
        ...

    async def summarize(self, messages: Sequence[LLMMessage]) -> SummaryDraft:
        """记忆压缩（M3-T03）：把旧对话消息压缩为结构化摘要草稿。

        Args:
            messages: 压缩窗口内的旧对话（已渲染为 LLMMessage，含工具结果）。

        Returns:
            摘要草稿（summary_text/key_entities/unresolved_questions/
            tool_results_cache）；轮次范围与 token 记账由压缩器补全。

        Raises:
            LLMUnavailableError: 超时/网络/服务端 5xx。
            LLMResponseError: 输出不是约定的严格 JSON 或字段校验失败。
        """
        ...
