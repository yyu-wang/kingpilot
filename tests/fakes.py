"""测试替身集合（M2-T01）。

测试设施，**不进 wheel**（pyproject packages 白名单不含 tests）。

FakeProvider 设计（D2 决策）：脚本化响应队列——每个方法一个队列，
出队即用；队列元素可以是正常值，也可以是 Exception 实例（出队即抛，
用于模拟 LLMUnavailableError / LLMResponseError 等异常路径）。

Why 队列而非固定返回：M2-T04 主链路与 M3 主循环的单测需要
「第一次规划成功、第二次超时」这类多轮剧本；固定返回表达不了时序。
队列耗尽抛 AssertionError——测试剧本不全应立刻暴露，而非静默重复
最后一个响应掩盖问题（fail-loud）。
"""

from collections.abc import Sequence
from typing import TypeVar

from agent.llm import TokenUsageSink
from infrastructure.exceptions import LLMResponseError, LLMUnavailableError
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
from models.tool import ToolExecution

_T = TypeVar("_T")

# 队列元素：正常返回值或待抛出的异常实例
PlanScriptItem = PlanResult | Exception
ReflectionScriptItem = ReflectionResult | Exception
ResponseScriptItem = str | Exception
SummaryScriptItem = SummaryDraft | Exception


class FakeProvider:
    """LLMProvider 的脚本化测试替身（满足 M2-T01 协议，供主循环单测复用）。"""

    def __init__(
        self,
        *,
        plan_queue: Sequence[PlanScriptItem] | None = None,
        reflection_queue: Sequence[ReflectionScriptItem] | None = None,
        response_queue: Sequence[ResponseScriptItem] | None = None,
        summarize_queue: Sequence[SummaryScriptItem] | None = None,
        usage_sink: TokenUsageSink | None = None,
    ) -> None:
        """Args:
        plan_queue: plan 依次出队的剧本；缺省为「无工具纯聊天」单元素剧本。
        reflection_queue: reflect 剧本；缺省为「不再续跑」。
        response_queue: generate_response 剧本；缺省为固定一句。
        summarize_queue: summarize 剧本（M3-T03）；缺省为一条固定摘要草稿。
        usage_sink: 用量回调（模块规范 4 通道，供测试断言上报次数）。
        """
        self._plan_queue: list[PlanScriptItem] = (
            list(plan_queue) if plan_queue else [PlanResult(tool_calls=[])]
        )
        self._reflection_queue: list[ReflectionScriptItem] = (
            list(reflection_queue)
            if reflection_queue
            else [ReflectionResult(continue_execution=False)]
        )
        self._response_queue: list[ResponseScriptItem] = (
            list(response_queue) if response_queue else ["（FakeProvider 默认答复）"]
        )
        self._summarize_queue: list[SummaryScriptItem] = (
            list(summarize_queue)
            if summarize_queue
            else [SummaryDraft(summary_text="（FakeProvider 默认压缩摘要）")]
        )
        self.usage_sink = usage_sink
        # 调用计数（测试断言调用次数用）
        self.plan_calls = 0
        self.reflect_calls = 0
        self.generate_calls = 0
        self.summarize_calls = 0
        # 最后一次调用的入参留痕（压缩器/记忆/反思断言用）
        self.last_plan_context: LLMContext | None = None
        self.last_plan_tools: list[ToolSpec] = []
        self.last_summarize_messages: list[LLMMessage] = []
        self.last_reflect_memory: MemorySnapshot | None = None
        self.last_reflect_summary: ExecutionSummary | None = None
        self.last_generate_context: LLMContext | None = None

    async def plan(self, context: LLMContext, tools: Sequence[ToolSpec]) -> PlanResult:
        self.plan_calls += 1
        self.last_plan_context = context
        self.last_plan_tools = list(tools)
        item = _next(self._plan_queue, "plan_queue")
        self._report_usage()
        if isinstance(item, Exception):
            raise item
        return item

    async def reflect(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> ReflectionResult:
        self.reflect_calls += 1
        self.last_reflect_memory = memory
        self.last_reflect_summary = execution
        item = _next(self._reflection_queue, "reflection_queue")
        self._report_usage()
        if isinstance(item, Exception):
            raise item
        return item

    async def generate_response(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> str:
        self.generate_calls += 1
        self.last_generate_context = context
        item = _next(self._response_queue, "response_queue")
        self._report_usage()
        if isinstance(item, Exception):
            raise item
        return item

    async def summarize(self, messages: Sequence[LLMMessage]) -> SummaryDraft:
        self.summarize_calls += 1
        self.last_summarize_messages = list(messages)
        item = _next(self._summarize_queue, "summarize_queue")
        self._report_usage()
        if isinstance(item, Exception):
            raise item
        return item

    def _report_usage(self) -> None:
        """每次 LLM 调用上报一次用量（模拟真实 Provider 的 usage 回传）。"""
        if self.usage_sink is not None:
            self.usage_sink(TokenUsage(prompt_tokens=100, completion_tokens=20))


def _next[T](queue: list[T], name: str) -> T:
    """出队下一个剧本元素；耗尽即 AssertionError（剧本不全，fail-loud）。"""
    if not queue:
        raise AssertionError(f"FakeProvider {name} 已耗尽：测试剧本不完整")
    return queue.pop(0)


# ── 常用剧本工厂（测试间复用，避免重复造）──


def unavailable_plan_provider() -> FakeProvider:
    """plan 阶段直接不可用（主循环降级路径的触发剧本）。"""
    return FakeProvider(plan_queue=[LLMUnavailableError("模拟超时")])


def bad_response_provider() -> FakeProvider:
    """响应不合法剧本（确定性错误，不触发降级）。"""
    return FakeProvider(response_queue=[LLMResponseError("JSON 解析失败")])


def execution_of(*tool_names: str) -> ExecutionSummary:
    """按工具名快速构造执行摘要（单测助手）。"""
    return ExecutionSummary(
        tool_executions=[ToolExecution(tool_name=name, parameters={}) for name in tool_names]
    )
