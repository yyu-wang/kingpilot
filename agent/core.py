"""Agent 主循环骨架（M3-T01）：Phase 0~6 串联 + AgentTrace 贯穿 + max_steps 熔断。

方案 §2.3 的骨架落地。本任务只立「阶段函数 + 串联 + 熔断」三根桩：
- **真实现**：Phase 1 规划 / Phase 3 执行 / Phase 4 反思与答复
  （plan/execute 复用 M2-T04 单轮内核的同款逻辑，归属本类内聚实现——
  不委托 QueryPipeline，避免「主循环调子管道再回调」的循环结构）
- **空实现扩展点**：Phase 0 记忆（T02）/ Phase 2 护栏（M4）/ Phase 5 幻觉
  校验（T06）/ Phase 6 记忆更新（T02）——空实现是「有签名的扩展点」，
  docstring 标注归属任务，保证骨架可串联、可单测、可被后续任务点对点替换，
  而非 ``pass`` 了事的死代码。

与 M2-T04 ``QueryPipeline`` 的关系（D1 决策）：
``QueryPipeline`` 是**单轮便捷入口**（一次 run 固定一轮，无 reflect），
面向「简单查询直连」场景保留；``AgentCore`` 是**多步主循环**，
在单轮内核外加 reflect 循环驱动 Plan-and-Solve 编排。两者 plan/execute
逻辑同源（同款实现），后续若演进为共享私有方法，改动集中在一处。
"""

import time
from typing import Any

from agent.llm import LLMProvider, TokenUsageSink
from agent.tools import ToolRegistry
from config.settings import Settings
from infrastructure.exceptions import KingPilotError
from kingdee.client import KingdeeClient
from models.agent import AgentResponse, AgentTrace
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    PlanResult,
    ReflectionResult,
)
from models.tool import ToolExecution
from models.user import UserContext


def _default_user_context() -> UserContext:
    """本地开发默认用户：最小权限角色（viewer 只读）。

    与 M2-T04 同源占位；正式用户体系与四层权限是 Phase 0（M4/M5）职责。
    """
    return UserContext(user_id="local_dev", role="viewer")


class AgentCore:
    """多步主循环：一次 run = Phase0~6，Phase 4 的 reflect 驱动多轮编排。"""

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        client: KingdeeClient,
        settings: Settings,
        usage_sink: TokenUsageSink | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._client = client
        self._settings = settings
        self._usage_sink = usage_sink

    async def run(self, user_input: str, user_context: UserContext | None = None) -> AgentResponse:
        """执行主循环，返回带完整 trace 的响应。

        Phase 1→3→4a 构成多步编排循环（reflect 说续跑就把 next_input 当新
        指令再跑一轮），``agent_max_steps`` 熔断；触顶给部分结果不抛错（D3）。

        Raises:
            LLMUnavailableError: LLM 不可用（降级策略归后续任务，本层传播）。
        """
        started = time.perf_counter()
        context_for_user = user_context or _default_user_context()
        trace = AgentTrace(
            user_input=user_input,
            user_context=context_for_user,
            user_id=context_for_user.user_id,
        )
        context = LLMContext(messages=[LLMMessage(role="user", content=user_input)])

        await self._phase0_memory(context, trace)

        # ── 多步编排循环：Phase 1 规划 → 2 护栏 → 3 执行 → 4a 反思 ──
        for _step in range(self._settings.agent_max_steps):
            plan = await self._phase1_plan(context, trace)
            await self._phase2_guard(plan, trace)
            await self._phase3_execute(plan, trace)
            reflection = await self._phase4_reflect(context, trace)
            trace.reflections.append(reflection.model_dump())
            if not reflection.continue_execution:
                break
            # 续跑：反思产出的下一指令作为新用户输入追加，进入下一轮规划
            context.messages.append(LLMMessage(role="user", content=reflection.next_input))
        else:
            # for-else：循环自然耗尽（reflect 一直说继续）= 触顶停步。
            # D3：不抛错，用已累积的部分结果继续走答复——用户该拿到部分结果。
            trace.used_fallback = True

        await self._phase4_generate(context, trace)
        await self._phase5_hallucination(trace)
        self._phase6_finalize(trace, started)
        return AgentResponse(content=trace.final_response, trace_id=trace.trace_id, trace=trace)

    # ── Phase 0：记忆管理（空实现扩展点）──

    async def _phase0_memory(self, context: LLMContext, trace: AgentTrace) -> None:
        """记忆管理层（方案支柱九）：压缩判断 + 数据域隔离 + 上下文组装。

        空实现（归 M3-T02/T03 记忆、M4 权限）：本任务仅保留扩展点签名。
        落地时在此做 ``needs_compression`` 判断、按角色过滤可见工具、
        把三层记忆摘要注入 ``context``。
        """

    # ── Phase 1：规划（真实现，复用 M2-T04 内核逻辑）──

    async def _phase1_plan(self, context: LLMContext, trace: AgentTrace) -> PlanResult:
        """规划层：全部工具声明给 LLM，产出本轮 tool_calls（防幻觉锚点注入归 T04/T06）。"""
        plan = await self._provider.plan(context, tools=self._tools.all_specs())
        trace.llm_planning = {
            "reasoning": plan.reasoning,
            "tool_calls": [decision.model_dump() for decision in plan.tool_calls],
        }
        return plan

    # ── Phase 2：安全护栏（空实现扩展点）──

    async def _phase2_guard(self, plan: PlanResult, trace: AgentTrace) -> None:
        """安全护栏层（方案支柱一~五）：Guard 1-5 逐个校验 tool_calls。

        空实现（归 M4）：本任务 tool_calls 直通。落地时在此插 AST 校验、
        风险分、四层权限、HITL 分级确认，拦截写 ``trace.guard_checks``。
        """

    # ── Phase 3：执行（真实现，复用 M2-T04 内核逻辑）──

    async def _phase3_execute(self, plan: PlanResult, trace: AgentTrace) -> None:
        """执行层：逐个执行 tool_calls，失败不中断（错误记入 ToolExecution 继续）。

        Saga 事务归 M5；本轮查询为只读，无需补偿。累积进 trace 供反思与答复。
        """
        for decision in plan.tool_calls:
            trace.tool_executions.append(
                await self._execute_one(decision.tool_name, decision.arguments)
            )

    async def _execute_one(self, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        """执行单个工具并归一为执行记录（成功带 result，失败带 error）。

        Why 只捕 KingPilotError：项目异常层次保证消息不含密钥（红线 3.6）；
        其他异常（编程错误）不吞，直接暴露。与 M2-T04 同源实现。
        """
        started = time.perf_counter()
        execution = ToolExecution(tool_name=tool_name, parameters=arguments)
        try:
            tool = self._tools.get(tool_name)
            result = await tool.execute(self._client, arguments)
            execution.result = result.model_dump()
        except KingPilotError as exc:
            execution.error = str(exc)
        execution.latency_ms = (time.perf_counter() - started) * 1000
        return execution

    # ── Phase 4：反思 + 答复（真实现；反思层完善归 T05）──

    async def _phase4_reflect(self, context: LLMContext, trace: AgentTrace) -> ReflectionResult:
        """反思层（骨架版）：把本轮执行摘要交给 LLM 判断是否续跑。

        完整反思层（结果是否足够/纠错）归 M3-T05；此处是最小多步编排语义。
        """
        summary = ExecutionSummary(tool_executions=trace.tool_executions, needs_more_steps=False)
        return await self._provider.reflect(context, summary, MemorySnapshot())

    async def _phase4_generate(self, context: LLMContext, trace: AgentTrace) -> None:
        """答复生成：用全部累积的工具执行记录生成自然语言答复。"""
        summary = ExecutionSummary(tool_executions=trace.tool_executions, needs_more_steps=False)
        trace.final_response = await self._provider.generate_response(
            context, summary, MemorySnapshot()
        )

    # ── Phase 5：幻觉校验（空实现扩展点）──

    async def _phase5_hallucination(self, trace: AgentTrace) -> None:
        """幻觉三重校验（方案支柱十）：响应 vs 工具结果 / 实体图 / 执行日志。

        空实现（归 M3-T06；纠错闭环 M4 就绪后补全）：本任务响应直通。
        落地时检出幻觉置 ``trace.hallucination_detected`` 并触发纠错循环。
        """

    # ── Phase 6：记忆更新 + 可观测性（部分实现）──

    def _phase6_finalize(self, trace: AgentTrace, started: float) -> None:
        """收尾：写总耗时与 token 用量；记忆更新（add_raw）归 M3-T02，落库归 M6。

        token 聚合（规范 4）：usage_sink 通道在 provider 层逐次调用上报，
        此处把 trace 上累积的字段定格；真用量聚合成 total 随记忆层一并补全。
        """
        trace.total_latency_ms = (time.perf_counter() - started) * 1000
