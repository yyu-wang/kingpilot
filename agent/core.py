"""Agent 主循环骨架（M3-T01）：Phase 0~6 串联 + AgentTrace 贯穿 + max_steps 熔断。

方案 §2.3 的骨架落地。本任务只立「阶段函数 + 串联 + 熔断」三根桩：
- **真实现**：Phase 1 规划 / Phase 3 执行 / Phase 4 反思与答复
  （plan/execute 复用 M2-T04 单轮内核的同款逻辑，归属本类内聚实现——
  不委托 QueryPipeline，避免「主循环调子管道再回调」的循环结构）
- **空实现扩展点**：Phase 2 护栏（M4）/ Phase 5 幻觉校验（T06）
  ——空实现是「有签名的扩展点」，docstring 标注归属任务，保证骨架可串联、
  可单测、可被后续任务点对点替换，而非 ``pass`` 了事的死代码。
- Phase 0/6 记忆随 M3-T03 落地：注入 ``MemoryStore`` 时启用压缩判定、
  快照组装与轮次写回；不注入则退化为 T01 的无状态单轮模式（既有测试零改动）。

与 M2-T04 ``QueryPipeline`` 的关系（D1 决策）：
``QueryPipeline`` 是**单轮便捷入口**（一次 run 固定一轮，无 reflect），
面向「简单查询直连」场景保留；``AgentCore`` 是**多步主循环**，
在单轮内核外加 reflect 循环驱动 Plan-and-Solve 编排。两者 plan/execute
逻辑同源（同款实现），后续若演进为共享私有方法，改动集中在一处。
"""

import time
from typing import Any

from loguru import logger

from agent.entity_extractor import extract_entities
from agent.llm import LLMProvider, TokenUsageSink
from agent.memory import MemoryStore, render_raw_for_llm
from agent.memory_compressor import maybe_compress
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
        memory: MemoryStore | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._client = client
        self._settings = settings
        self._usage_sink = usage_sink
        # None = T01 无状态模式（不压缩/不组装历史/不写回），既有单测零改动
        self._memory = memory

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
        context, snapshot = await self._phase0_memory(user_input, trace)

        # ── 多步编排循环：Phase 1 规划 → 2 护栏 → 3 执行 → 4a 反思 ──
        for _step in range(self._settings.agent_max_steps):
            plan = await self._phase1_plan(context, trace)
            await self._phase2_guard(plan, trace)
            await self._phase3_execute(plan, trace)
            reflection = await self._phase4_reflect(context, trace, snapshot)
            trace.reflections.append(reflection.model_dump())
            if not reflection.continue_execution:
                break
            # 续跑：反思产出的下一指令作为新用户输入追加，进入下一轮规划
            context.messages.append(LLMMessage(role="user", content=reflection.next_input))
        else:
            # for-else：循环自然耗尽（reflect 一直说继续）= 触顶停步。
            # D3：不抛错，用已累积的部分结果继续走答复——用户该拿到部分结果。
            trace.used_fallback = True

        await self._phase4_generate(context, trace, snapshot)
        await self._phase5_hallucination(trace)
        self._phase6_finalize(trace, started, user_input)
        return AgentResponse(content=trace.final_response, trace_id=trace.trace_id, trace=trace)

    # ── Phase 0：记忆管理（M3-T03 压缩/组装；数据域隔离归 M4）──

    async def _phase0_memory(
        self, user_input: str, trace: AgentTrace
    ) -> tuple[LLMContext, MemorySnapshot]:
        """压缩判定 → 三层快照 → 组装本轮 LLM 上下文。

        无 MemoryStore（无状态模式）：只装当前一句，快照为空——T01 行为。
        有 MemoryStore：先 ``maybe_compress``（内部处理三触发与失败放行），
        再按「system(摘要+画像) → L1 历史 → 当前输入」拼装；实体图（L3）
        经 anchors 注入归 M3-T04，本任务只铺三层中的摘要与原文。
        """
        if self._memory is None:
            context = LLMContext(messages=[LLMMessage(role="user", content=user_input)])
            return context, MemorySnapshot()

        await maybe_compress(self._provider, self._memory, self._settings)
        snapshot = self._memory.to_snapshot()
        messages: list[LLMMessage] = []
        system_parts: list[str] = []
        if snapshot.session_summary:
            system_parts.append(f"【会话历史摘要】\n{snapshot.session_summary}")
        if snapshot.user_profile:
            system_parts.append(f"【用户画像】\n{snapshot.user_profile}")
        if system_parts:
            messages.append(LLMMessage(role="system", content="\n\n".join(system_parts)))
        # 历史不含当前输入：当前轮在 Phase 6 才写回，时序不重不漏
        messages.extend(render_raw_for_llm(self._memory.raw_messages()))
        messages.append(LLMMessage(role="user", content=user_input))
        trace.session_id = self._memory.session_id
        # Layer 3 实体图经 anchors 注入 LLM——「之前提到的客户」可回答性来源
        context = LLMContext(messages=messages)
        context.anchors.extend(self._memory.entity_anchors())
        return context, snapshot

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

    async def _phase4_reflect(
        self, context: LLMContext, trace: AgentTrace, snapshot: MemorySnapshot
    ) -> ReflectionResult:
        """反思层（骨架版）：把本轮执行摘要交给 LLM 判断是否续跑。

        完整反思层（结果是否足够/纠错）归 M3-T05；此处是最小多步编排语义。
        """
        summary = ExecutionSummary(tool_executions=trace.tool_executions, needs_more_steps=False)
        return await self._provider.reflect(context, summary, snapshot)

    async def _phase4_generate(
        self, context: LLMContext, trace: AgentTrace, snapshot: MemorySnapshot
    ) -> None:
        """答复生成：用全部累积的工具执行记录生成自然语言答复。"""
        summary = ExecutionSummary(tool_executions=trace.tool_executions, needs_more_steps=False)
        trace.final_response = await self._provider.generate_response(context, summary, snapshot)

    # ── Phase 5：幻觉校验（空实现扩展点）──

    async def _phase5_hallucination(self, trace: AgentTrace) -> None:
        """幻觉三重校验（方案支柱十）：响应 vs 工具结果 / 实体图 / 执行日志。

        空实现（归 M3-T06；纠错闭环 M4 就绪后补全）：本任务响应直通。
        落地时检出幻觉置 ``trace.hallucination_detected`` 并触发纠错循环。
        """

    # ── Phase 6：记忆写回 + 可观测性（M3-T03 落地写回；落库归 M6）──

    def _phase6_finalize(self, trace: AgentTrace, started: float, user_input: str) -> None:
        """收尾：写总耗时；有记忆时把本轮一轮对话写回 Layer 1。

        写回口径：原始 user 输入 + 每条工具执行（成功带结构化结果、失败带
        失败文本）+ 最终 assistant 答复。reflect 续跑的 next_input 是 agent
        自生成的中间指令而非用户发言，不写入（否则虚增轮数污染压缩窗口）。
        token 聚合随 M6/M7 成本治理接入 trace.llm_token_usage。
        """
        trace.total_latency_ms = (time.perf_counter() - started) * 1000
        if self._memory is None:
            return
        self._memory.add_raw("user", user_input)
        current_round = self._memory.raw_rounds  # user 写入后即为当前轮号
        for execution in trace.tool_executions:
            if execution.error is None:
                result = execution.result or {}
                self._memory.add_raw("tool", "", structured_result=result)
                # 工具结构化结果 → Layer 3 实体图；抽取异常不阻断主对话
                try:
                    for entity in extract_entities(result, current_round):
                        self._memory.upsert_entity(entity)
                except Exception as exc:
                    logger.warning(
                        "实体抽取失败，跳过本轮入图: tool={} error={}", execution.tool_name, exc
                    )
            else:
                self._memory.add_raw(
                    "tool", f"工具 {execution.tool_name} 执行失败：{execution.error}"
                )
        self._memory.add_raw("assistant", trace.final_response)
