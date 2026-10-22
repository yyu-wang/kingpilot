"""Agent 主循环骨架（M3-T01）：Phase 0~6 串联 + AgentTrace 贯穿 + max_steps 熔断。

方案 §2.3 的骨架落地。本类按阶段函数串联：
- **真实现**：Phase 1 规划 / Phase 3 执行 / Phase 4 反思与答复
  （plan/execute 复用 M2-T04 单轮内核的同款逻辑，归属本类内聚实现——
  不委托 QueryPipeline，避免「主循环调子管道再回调」的循环结构）
- **Phase 5 幻觉检测随 M3-T06 落地**：规则化三重校验，只置标志不纠错
  （自动纠错循环暂未启用，见 M4-T10 决策）
- **Phase 2 安全审查随 M4-T10 落地**：注入 ``SafetyLayer`` 后启用
  工具过滤 + Guard 1~5 + 权限 + 风险 + HITL；不注入则退化为直通，
  保证既有单测零改动。
- Phase 0/6 记忆随 M3-T03 落地：注入 ``MemoryStore`` 时启用压缩判定、
  快照组装与轮次写回；不注入则退化为 T01 的无状态单轮模式（既有测试零改动）。

与 M2-T04 ``QueryPipeline`` 的关系（D1 决策）：
``QueryPipeline`` 是**单轮便捷入口**（一次 run 固定一轮，无 reflect），
面向「简单查询直连」场景保留；``AgentCore`` 是**多步主循环**，
在单轮内核外加 reflect 循环驱动 Plan-and-Solve 编排。两者 plan/execute
逻辑同源（同款实现），后续若演进为共享私有方法，改动集中在一处。
"""

import re
import time
from collections.abc import Sequence

from loguru import logger
from pydantic import ValidationError

from agent.entity_extractor import extract_entities
from agent.events import AgentEvent, AgentEventSink, EventStatus
from agent.hallucination import detect_hallucinations
from agent.knowledge import KB_SEARCH_TOOL
from agent.llm import LLMProvider, TokenUsageSink
from agent.memory import MemoryStore, render_raw_for_llm
from agent.memory_compressor import maybe_compress
from agent.reflection import observation_messages, reflect_step, should_reflect
from agent.rule_engine import RuleEngine
from agent.tools import ToolRegistry
from config.rag_rules import KNOWLEDGE_NO_HIT_MESSAGE, KNOWLEDGE_NOT_SEARCHED_MESSAGE
from config.settings import Settings
from erp.protocol import ErpClient
from governance.rag.answer import enforce_citations, render_knowledge_fallback
from governance.semantic_cache import SemanticCache, build_cache_key, is_cacheable_plan
from infrastructure.exceptions import KingPilotError, LLMUnavailableError, TraceStoreError
from infrastructure.tenancy import tenant_scope
from models.agent import AgentResponse, AgentTrace
from models.audit import AuditEvent
from models.kingdee import BillQueryResult
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    PlanResult,
    ReflectionResult,
    ToolSpec,
)
from models.rag import KnowledgeSearchResult
from models.tenant import TenantContext
from models.tool import ToolExecution
from models.user import UserContext
from observability.audit import AuditLogger
from observability.tracing import TraceStore
from safety.datasecurity import redact_rows
from safety.dlp import DlpScanner, DlpScanResult
from safety.engine import GuardedCall, SafetyLayer, SafetyVerdict
from safety.permission_engine import apply_column_filter

_FALLBACK_NOTICE = "（LLM 暂不可用，已切换到基础查询模式）"
_UNAVAILABLE_MESSAGE = (
    "LLM 暂不可用，基础查询模式暂未理解该请求。"
    "请提供客户编码、单据编号或物料编码等明确条件；写操作请稍后重试。"
)
_FALLBACK_MAX_ROWS = 10
_DLP_BLOCK_CONTENT = "响应包含敏感个人信息，已按数据安全策略阻断发送。请调整查询条件后重试。"
# LLM 偶发空答复时的兜底文案（先重试一次，仍为空才使用）
_EMPTY_RESPONSE_FALLBACK = "抱歉，本次没有生成有效答复，请重试或换个问法。"
# 显式查询动词：LLM 空规划时，仅对「查单号」请求做规则补全
_EXPLICIT_QUERY_VERBS = ("查", "看一下", "列出", "显示")
# 明确业务编码：没有编码的「查客户联系人」等宽泛请求不自动补全，避免误判
_EXPLICIT_CODE_RE = re.compile(r"\b(?:SO|PO|CGRK|CUS|SUP|MAT)\d+\b", re.IGNORECASE)

# 规划层产品提示（M9 修复）：锚定身份/能力/追问/知识检索，避免模型自我降级
_PLANNER_PROMPT_MARK = "你是 KingPilot，企业级 ERP 智能助手。"
_PLANNER_SYSTEM_PROMPT = (
    f"{_PLANNER_PROMPT_MARK}"
    "你的能力范围：1) 查询销售订单、采购订单、采购入库单、客户、供应商、即时库存；"
    "2) 通过审批流执行创建、提交、审核、反审核、删除；3) 用 kb_search 检索企业内部操作手册。"
    "行为要求：A. 用户明确要求执行写操作且参数齐全时，必须调用对应写工具，"
    "不得声称自己没有该能力；B. 缺少必填参数时，用一句话追问缺少的字段，"
    "不要提前调用写工具；C. 涉及流程、规范、时效、操作步骤的问题，先调用 kb_search，"
    "不得仅凭常识作答；D. 不得编造单号、金额、工具或功能。"
)
# 含疑问标记时不做写意图覆盖，避免把「审核需要什么条件？」误判为执行审核
_QUESTION_MARKERS = (
    "吗",
    "么",
    "如何",
    "怎么",
    "怎样",
    "能否",
    "可以",
    "需要",
    "条件",
    "流程",
    "规则",
    "为什么",
    "？",
    "?",
)


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
        client: ErpClient,
        settings: Settings,
        usage_sink: TokenUsageSink | None = None,
        memory: MemoryStore | None = None,
        safety: SafetyLayer | None = None,
        rule_engine: RuleEngine | None = None,
        trace_store: TraceStore | None = None,
        audit: AuditLogger | None = None,
        cache: SemanticCache | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._client = client
        self._settings = settings
        self._usage_sink = usage_sink
        # None = T01 无状态模式（不压缩/不组装历史/不写回），既有单测零改动
        self._memory = memory
        # None = 不带安全层（单测/骨架模式）；生产入口必须注入（T10）
        self._safety = safety
        # LLM 不可用时的只读兜底（M6-T02），默认启用
        self._rule_engine = rule_engine or RuleEngine()
        # None = 不落盘 trace；生产入口注入后所有出口都会保存（M6-T03）
        self._trace_store = trace_store
        # None = DLP 命中时只写告警日志；生产入口注入后进统一审计流（M7-T02）
        self._audit = audit
        # 出域扫描默认开启（fail-closed）：所有响应在返回前统一过 DLP
        self._dlp = DlpScanner()
        # 只读计划缓存默认开启（M7-T04）；注入供测试控制 TTL/时钟/容量
        self._cache = cache or SemanticCache(
            ttl_seconds=settings.semantic_cache_ttl_seconds,
            max_entries=settings.semantic_cache_max_entries,
        )
        # None = 非流式路径（行为零变化）；SSE 请求注入 queue.put_nowait（M9-T02）
        self._event_sink = event_sink

    def _emit(self, event: AgentEvent) -> None:
        """事件是旁路：sink 自身失败只告警，绝不打断主循环。"""
        if self._event_sink is None:
            return
        try:
            self._event_sink(event)
        except Exception as exc:
            logger.warning("事件推送失败（不影响主循环）: {}", exc)

    def _phase(self, phase: str, status: EventStatus) -> None:
        """阶段事件简写：只暴露阶段名与状态，不携带业务数据。"""
        self._emit(AgentEvent(event="phase", phase=phase, status=status))

    async def run(self, user_input: str, user_context: UserContext | None = None) -> AgentResponse:
        """执行主循环、出域扫描并落盘 trace；落盘失败只告警，不影响用户响应。

        M9-T11：整个运行进入租户上下文，使审计/成本/存储等旁路按租户隔离。
        """
        user = user_context or _default_user_context()
        with tenant_scope(TenantContext.from_user(user).tenant_id):
            response = await self._run(user_input, user)
            # 出域扫描在 Trace 落盘之前：磁盘上只保留净化后的答复（M7-T02）
            response = self._enforce_dlp(response)
            if self._trace_store is not None and response.trace is not None:
                try:
                    self._trace_store.save(response.trace)
                except TraceStoreError as exc:
                    logger.warning("Trace 落盘失败（不影响响应）: {}", exc)
            return response

    def _enforce_dlp(self, response: AgentResponse) -> AgentResponse:
        """响应发送前的 DLP 出域扫描：命中即整条阻断并审计。

        不做「局部打码后返回」——部分保留可能被拼接绕过；命中时 Trace
        的 ``final_response`` 同步替换，保证原文不落盘。
        """
        result = self._dlp.scan(response.content)
        if not result.blocked:
            return response

        if response.trace is not None:
            response.trace.final_response = _DLP_BLOCK_CONTENT
        # Phase 6 已把原始答复写进记忆；阻断后同步替换，防止再次进入上下文
        if self._memory is not None:
            self._memory.replace_last_assistant(_DLP_BLOCK_CONTENT)
        self._emit_dlp_audit(response, result)
        logger.warning(
            "DLP 出域拦截 | trace_id={} categories={} total={}",
            response.trace_id,
            result.categories,
            result.total,
        )
        return response.model_copy(
            update={
                "content": _DLP_BLOCK_CONTENT,
                "blocked": True,
                "reason": _DLP_BLOCK_CONTENT,
                "hallucination_warning": False,
            }
        )

    def _emit_dlp_audit(self, response: AgentResponse, result: DlpScanResult) -> None:
        """写 ``dlp.blocked`` 审计事件；detail 只记类别与数量，不记原值。"""
        if self._audit is None:
            return
        trace = response.trace
        self._audit.emit(
            AuditEvent(
                event_type="dlp.blocked",
                trace_id=str(response.trace_id),
                user_id=trace.user_id if trace is not None else "",
                user_role=trace.user_context.role if trace is not None else "",
                ip_address=trace.user_context.ip_address if trace is not None else "",
                status="blocked",
                detail={
                    "categories": list(result.categories),
                    "total": result.total,
                },
            )
        )

    async def _run(self, user_input: str, user_context: UserContext | None = None) -> AgentResponse:
        """执行主循环，返回带完整 trace 的响应。

        Phase 1→3→4a 构成多步编排循环（reflect 说续跑就把 next_input 当新
        指令再跑一轮），``agent_max_steps`` 熔断；触顶给部分结果不抛错（D3）。

        LLM 不可用（``LLMUnavailableError``）时走只读规则引擎兜底（M6-T02），
        不再向上抛异常。
        """
        started = time.perf_counter()
        context_for_user = user_context or _default_user_context()
        trace = AgentTrace(
            user_input=user_input,
            user_context=context_for_user,
            user_id=context_for_user.user_id,
        )
        context, snapshot = await self._phase0_memory(user_input, trace)
        last_next_input = ""

        # ── 多步编排循环：Phase 1 规划 → 2 护栏 → 3 执行 → 4a 反思 ──
        for _step in range(self._settings.agent_max_steps):
            self._phase("plan", "start")
            try:
                plan = await self._phase1_plan(context, trace, context_for_user)
            except LLMUnavailableError as exc:
                self._phase("plan", "error")
                if trace.tool_executions:
                    # 后续步骤规划失败：已有结果足够，直接确定性回答，避免重复查询
                    return self._finish_fallback(
                        trace,
                        started,
                        user_input,
                        message="LLM 暂不可用，已根据已完成的查询结果回答。",
                    )
                return await self._fallback_from_plan(
                    user_input, trace, context_for_user, started, reason=str(exc)
                )
            self._phase("plan", "success")
            if not plan.tool_calls:
                # 无权限的工具不会下发给 LLM，模型只能回「没查到」；这里按
                # 用户意图反查目标工具，直接给出明确的权限不足提示
                denied = self._permission_denial_message(user_input, context_for_user)
                if denied is not None:
                    return self._finish_denied(trace, started, user_input, denied)
            self._phase("guard", "start")
            verdict = await self._phase2_guard(plan, trace, context_for_user)
            self._phase("guard", "success")
            if not verdict.proceed:
                # 拦截/取消/审批待处理：不执行、不反思、不生成答复，直接收尾
                trace.final_response = verdict.message
                trace.approval_id = verdict.approval_id
                self._phase6_finalize(trace, started, user_input)
                return AgentResponse(
                    content=verdict.message,
                    trace_id=trace.trace_id,
                    blocked=verdict.blocked,
                    reason=verdict.message,
                    cancelled=verdict.cancelled,
                    hallucination_warning=False,
                    approval_pending=verdict.approval_pending,
                    approval_id=verdict.approval_id,
                    trace=trace,
                )
            step_start = len(trace.tool_executions)
            if verdict.calls:
                self._phase("execute", "start")
                await self._phase3_execute(verdict.calls, trace)
                self._phase("execute", "success")
            step_executions = trace.tool_executions[step_start:]
            # 观察回灌：本步工具结果进上下文，本轮反思与下一轮规划都看得见
            context.messages.extend(observation_messages(step_executions))
            if not should_reflect(step_executions):
                break  # 纯聊天无可观察结果，不浪费一次反思调用
            self._phase("reflect", "start")
            try:
                reflection = await self._phase4_reflect(
                    context, step_executions, snapshot, last_next_input
                )
            except LLMUnavailableError:
                self._phase("reflect", "error")
                return self._finish_fallback(trace, started, user_input)
            self._phase("reflect", "success")
            trace.reflections.append(reflection.model_dump())
            if not reflection.continue_execution:
                break
            # 续跑：反思产出的下一指令作为新用户输入追加，进入下一轮规划
            last_next_input = reflection.next_input
            context.messages.append(LLMMessage(role="user", content=reflection.next_input))
        else:
            # for-else：循环自然耗尽（reflect 一直说继续）= 触顶停步。
            # D3：不抛错，用已累积的部分结果继续走答复——用户该拿到部分结果。
            trace.used_fallback = True

        self._phase("generate", "start")
        try:
            await self._phase4_generate(context, trace, snapshot)
        except LLMUnavailableError:
            self._phase("generate", "error")
            return self._finish_fallback(trace, started, user_input)
        self._phase("generate", "success")
        self._phase4_citation(trace)
        if not trace.final_response.strip():
            trace.final_response = _EMPTY_RESPONSE_FALLBACK
            trace.used_fallback = True
            logger.warning("答复为空且无知识兜底，使用通用文案 | trace_id={}", trace.trace_id)
        self._phase5_hallucination(trace)
        self._phase6_finalize(trace, started, user_input)
        return AgentResponse(
            content=trace.final_response,
            trace_id=trace.trace_id,
            hallucination_warning=trace.hallucination_detected and not trace.hallucination_resolved,
            trace=trace,
        )

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

    async def _phase1_plan(
        self, context: LLMContext, trace: AgentTrace, user_context: UserContext
    ) -> PlanResult:
        """规划层：声明可用工具，产出本轮 tool_calls。

        首轮只读计划走语义缓存（M7-T04）：命中不调 LLM；
        续跑计划依赖上一步观察，不读写缓存。
        有安全层时只下发角色可见工具（M4-T08 拦截点 1）；
        无安全层时保持 T01 行为（全部工具）。
        """
        cache_key = ""
        if not trace.tool_executions and not trace.reflections:
            cache_key = self._cache_key(trace.user_input, user_context)
            cached = self._cache.get(cache_key)
            if cached is not None:
                trace.llm_planning = {
                    "cache_hit": True,
                    "reasoning": cached.reasoning,
                    "tool_calls": [decision.model_dump() for decision in cached.tool_calls],
                }
                return cached

        plan = await self._provider.plan(
            self._with_planner_prompt(context), tools=self._visible_specs(user_context)
        )
        trace.llm_planning = {
            "reasoning": plan.reasoning,
            "tool_calls": [decision.model_dump() for decision in plan.tool_calls],
        }
        plan = self._prefer_explicit_write_plan(plan, trace, user_context)
        plan = self._complete_empty_plan(plan, trace, user_context)
        if cache_key and is_cacheable_plan(plan, self._tool_types()):
            self._cache.set(cache_key, plan)
        return plan

    @staticmethod
    def _with_planner_prompt(context: LLMContext) -> LLMContext:
        """注入产品提示（已存在则跳过），不改动原始上下文。

        插在已有 system 消息之后，保留「摘要/画像在前」的既有顺序。
        """
        if any(
            message.role == "system" and message.content.startswith(_PLANNER_PROMPT_MARK)
            for message in context.messages
        ):
            return context
        messages = list(context.messages)
        insert_at = 0
        while insert_at < len(messages) and messages[insert_at].role == "system":
            insert_at += 1
        messages.insert(insert_at, LLMMessage(role="system", content=_PLANNER_SYSTEM_PROMPT))
        return LLMContext(messages=messages, anchors=list(context.anchors))

    def _prefer_explicit_write_plan(
        self, plan: PlanResult, trace: AgentTrace, user_context: UserContext
    ) -> PlanResult:
        """显式写动作优先：LLM 错选查询工具时，用规则引擎纠正为写工具。

        Why：采购订单「审核 POxxxxxx」被模型规划成查询，导致订单停在审核中、
        后续反审核/删除全部失败。这里只在模型没有选中任何写工具、且用户输入
        是「动作开头 + 唯一单号」时覆盖；带疑问词的咨询类输入不覆盖。
        """
        if not plan.tool_calls:
            return plan
        tool_types = self._tool_types()
        if any(tool_types.get(call.tool_name) == "write" for call in plan.tool_calls):
            return plan
        text = trace.user_input.strip()
        if any(marker in text for marker in _QUESTION_MARKERS):
            return plan
        available = {spec.name for spec in self._visible_specs(user_context)}
        matched = self._rule_engine.match_write_plan(text, available_tools=available)
        if matched is None:
            return plan
        trace.llm_planning["rule_override"] = {
            "intent": matched.intent,
            "reason": "explicit_write_action",
        }
        trace.llm_planning["tool_calls"] = [
            decision.model_dump() for decision in matched.plan.tool_calls
        ]
        logger.info(
            "显式写动作覆盖只读计划 | trace_id={} intent={}",
            trace.trace_id,
            matched.intent,
        )
        return matched.plan

    def _complete_empty_plan(
        self, plan: PlanResult, trace: AgentTrace, user_context: UserContext
    ) -> PlanResult:
        """LLM 空规划时，对「动作 + 明确单号」与「查 + 明确业务编码」做确定性补全。

        Why：小模型/本地模型偶尔不调用工具，直接输出无命中话术，导致
        「查一下销售订单 SO000001」这类明确查询被误判为知识问题；
        多轮上下文里同样会漏掉写工具，直接编造「已提交审批」——实际没有审批单
        （经理账号看不到卡片）。写意图优先补全为真实写工具调用，继续走
        安全审查与审批；创建类参数多，仍交给 LLM。只处理首轮请求。
        """
        if plan.tool_calls or trace.tool_executions or trace.reflections:
            return plan
        available = {spec.name for spec in self._visible_specs(user_context)}
        matched = self._rule_engine.match_write_plan(trace.user_input, available_tools=available)
        if matched is None:
            has_query_verb = any(verb in trace.user_input for verb in _EXPLICIT_QUERY_VERBS)
            if has_query_verb and _EXPLICIT_CODE_RE.search(trace.user_input):
                matched = self._rule_engine.match(trace.user_input, available_tools=available)
        if matched is None:
            # 知识意图但没有调 kb_search：补一次检索，保证有引用来源
            matched = self._rule_engine.match_knowledge_plan(
                trace.user_input, available_tools=available
            )
        if matched is None:
            return plan
        trace.llm_planning["rule_fallback"] = {
            "intent": matched.intent,
            "reason": "knowledge_intent" if matched.intent == "knowledge" else "llm_plan_empty",
        }
        trace.llm_planning["tool_calls"] = [
            decision.model_dump() for decision in matched.plan.tool_calls
        ]
        logger.info(
            "LLM 空规划，规则引擎补全 | trace_id={} intent={}",
            trace.trace_id,
            matched.intent,
        )
        return matched.plan

    def _cache_key(self, user_input: str, user_context: UserContext) -> str:
        """缓存键：问题 + 可见工具 + 角色/数据域。"""
        return build_cache_key(
            user_input,
            tool_names=[spec.name for spec in self._visible_specs(user_context)],
            scope={
                "role": user_context.role,
                "data_scope": user_context.data_scope,
            },
        )

    def _tool_types(self) -> dict[str, str]:
        """工具名 → 服务端工具类型（只读判定依据）。"""
        return {tool.name: tool.tool_type for tool in self._tools.all_tools()}

    def _visible_specs(self, user_context: UserContext) -> list[ToolSpec]:
        """当前用户可见的工具声明（安全层过滤 / 全部）。"""
        if self._safety is not None:
            return self._safety.visible_tools(user_context)
        return self._tools.all_specs()

    def _permission_denial_message(self, user_input: str, user_context: UserContext) -> str | None:
        """空规划时反查目标工具：无权限则返回明确拒绝文案。

        无权限的工具在拦截点 1 就被过滤掉，LLM 看不到也不会调用，
        其结果往往是「没有查询到数据」，甚至直接编造「已创建/已删除」。
        这里用规则引擎推断用户想查/想写哪类数据，再走权限预检给出准确答复。
        """
        if self._safety is None:
            return None
        tool_name = self._rule_engine.match_tool_scope(user_input)
        if tool_name is None:
            # 写意图：写工具被数据域过滤后 LLM 看不到，可能直接编造「已创建/已删除」
            tool_name = self._rule_engine.match_write_tool_scope(user_input)
        if tool_name is None:
            return None
        visible = {spec.name for spec in self._visible_specs(user_context)}
        if tool_name in visible:
            return None
        return self._safety.permission_denial(user_context, tool_name)

    # ── LLM 不可用：只读降级（M6-T02）──

    async def _fallback_from_plan(
        self,
        user_input: str,
        trace: AgentTrace,
        user_context: UserContext,
        started: float,
        *,
        reason: str,
    ) -> AgentResponse:
        """规则引擎兜底：只读计划 → 同一套安全审查 → 执行 → 确定性答复。"""
        specs = self._visible_specs(user_context)
        matched = self._rule_engine.match(user_input, available_tools={spec.name for spec in specs})
        trace.used_fallback = True
        trace.llm_planning = {
            "fallback": True,
            "reason": reason,
            "intent": matched.intent if matched is not None else "",
        }
        if matched is None:
            # LLM 不可用时同样先判权限：目标工具存在但当前角色不可见时，
            # 直接说明「权限不足」，不要笼统回「基础查询模式暂未理解」
            denied = self._permission_denial_message(user_input, user_context)
            if denied is not None:
                trace.llm_planning["permission_denied"] = True
                return self._finish_denied(trace, started, user_input, denied)
            return self._finish_fallback(trace, started, user_input)

        self._phase("guard", "start")
        verdict = await self._phase2_guard(matched.plan, trace, user_context)
        self._phase("guard", "success")
        if not verdict.proceed:
            return self._finish_fallback(
                trace,
                started,
                user_input,
                message=verdict.message,
                blocked=verdict.blocked,
                cancelled=verdict.cancelled,
            )
        if verdict.calls:
            self._phase("execute", "start")
            await self._phase3_execute(verdict.calls, trace)
            self._phase("execute", "success")
        return self._finish_fallback(trace, started, user_input)

    def _finish_denied(
        self,
        trace: AgentTrace,
        started: float,
        user_input: str,
        message: str,
    ) -> AgentResponse:
        """权限预检拒绝：直接返回「权限不足」，不调 LLM、不执行工具。"""
        trace.final_response = message
        trace.permission_check = {
            **trace.permission_check,
            "allowed": False,
            "reason": message,
            "denied_by": "permission_precheck",
        }
        self._phase6_finalize(trace, started, user_input)
        return AgentResponse(
            content=message,
            trace_id=trace.trace_id,
            blocked=True,
            reason=message,
            hallucination_warning=False,
            trace=trace,
        )

    def _finish_fallback(
        self,
        trace: AgentTrace,
        started: float,
        user_input: str,
        *,
        message: str = "",
        blocked: bool = False,
        cancelled: bool = False,
    ) -> AgentResponse:
        """确定性收尾：不调 LLM、不做反思/幻觉检测，直接渲染已执行结果。"""
        trace.used_fallback = True
        if message:
            detail = message
            if trace.tool_executions:
                detail = f"{message}\n{render_fallback_response(trace.tool_executions)}"
        elif trace.tool_executions:
            detail = render_fallback_response(trace.tool_executions)
        else:
            detail = _UNAVAILABLE_MESSAGE
        content = f"{_FALLBACK_NOTICE}\n{detail}"
        trace.final_response = content
        # 降级路径同样强制引用/无命中兜底，避免绕开知识安全策略
        self._phase4_citation(trace)
        content = trace.final_response
        self._phase6_finalize(trace, started, user_input)
        return AgentResponse(
            content=content,
            trace_id=trace.trace_id,
            blocked=blocked,
            reason=content,
            cancelled=cancelled,
            hallucination_warning=False,
            trace=trace,
        )

    # ── Phase 2：安全护栏（M4-T10：Guard + 权限 + 风险 + HITL）──

    async def _phase2_guard(
        self,
        plan: PlanResult,
        trace: AgentTrace,
        user_context: UserContext,
    ) -> SafetyVerdict:
        """安全审查：任一条拦截/取消则整批不执行。

        无安全层时直接放行（单测/骨架模式的 T01 行为）。
        """
        if self._safety is None:
            return SafetyVerdict(
                proceed=True,
                calls=tuple(
                    GuardedCall(
                        tool_name=decision.tool_name,
                        arguments=dict(decision.arguments),
                    )
                    for decision in plan.tool_calls
                ),
            )

        verdict = await self._safety.review(
            user_context,
            plan.tool_calls,
            str(trace.trace_id),
            session_id=trace.session_id,
        )
        trace.guard_checks.extend(verdict.guard_records)
        if verdict.risk is not None:
            trace.risk_assessment = verdict.risk.to_dict()
        if verdict.permission is not None:
            trace.permission_check = verdict.permission.to_dict()
        if verdict.hitl is not None:
            trace.risk_assessment["hitl"] = verdict.hitl.to_dict()
        return verdict

    # ── Phase 3：执行（真实现，复用 M2-T04 内核逻辑）──

    async def _phase3_execute(self, calls: Sequence[GuardedCall], trace: AgentTrace) -> None:
        """执行层：逐个执行已审查调用，失败不中断（错误记入 ToolExecution 继续）。

        Saga 事务归 M5；本轮查询为只读，无需补偿。累积进 trace 供反思与答复。
        """
        for call in calls:
            self._emit(AgentEvent(event="tool", tool_name=call.tool_name, status="start"))
            execution = await self._execute_one(call)
            self._emit(
                AgentEvent(
                    event="tool",
                    tool_name=call.tool_name,
                    status="error" if execution.error else "success",
                    latency_ms=execution.latency_ms,
                )
            )
            trace.tool_executions.append(execution)

    async def _execute_one(self, call: GuardedCall) -> ToolExecution:
        """执行单个已审查调用（含行过滤注入与列字段剔除）。

        Why 只捕 KingPilotError：项目异常层次保证消息不含密钥（红线 3.6）；
        其他异常（编程错误）不吞，直接暴露。与 M2-T04 同源实现。
        """
        started = time.perf_counter()
        arguments = dict(call.arguments)
        execution = ToolExecution(tool_name=call.tool_name, parameters=arguments)
        try:
            tool = self._tools.get(call.tool_name)
            if call.row_filter.conditions:
                result = await tool.execute(self._client, arguments, row_filter=call.row_filter)
            else:
                result = await tool.execute(self._client, arguments)
            payload = result.model_dump()
            # ERP 结果才做列级权限与字段脱敏；知识库结果不含 ERP 字段
            if isinstance(result, BillQueryResult):
                rows = result.rows
                if call.denied_fields:
                    # 列级权限：结果出口统一剔除禁用字段（M4-T08）
                    rows = apply_column_filter(rows, call.denied_fields)
                # 数据安全：敏感字段在进入 Trace / LLM / 记忆前统一脱敏（M7-T01）
                payload["rows"] = redact_rows(rows)
            execution.result = payload
        except KingPilotError as exc:
            execution.error = str(exc)
        execution.latency_ms = (time.perf_counter() - started) * 1000
        return execution

    # ── Phase 4：反思 + 答复（M3-T05 反思层）──

    async def _phase4_reflect(
        self,
        context: LLMContext,
        executions: Sequence[ToolExecution],
        snapshot: MemorySnapshot,
        previous_next_input: str,
    ) -> ReflectionResult:
        """反思层：本步执行结果交 LLM 判断是否续跑，续跑指令经防御归一化。

        只传本步执行记录（非全量 trace）——反思针对「刚才这批结果」，
        历史步骤的结论已沉淀在上一轮反思与上下文中。
        """
        return await reflect_step(
            self._provider,
            context,
            executions,
            snapshot,
            previous_next_input=previous_next_input,
        )

    async def _phase4_generate(
        self, context: LLMContext, trace: AgentTrace, snapshot: MemorySnapshot
    ) -> None:
        """答复生成：用全部累积的工具执行记录生成自然语言答复。

        LLM 偶发返回空字符串（DeepSeek 长上下文下出现过）：重试一次，
        仍为空则交给 `_run` 的兜底文案，避免前端收到空 final。
        """
        summary = ExecutionSummary(tool_executions=trace.tool_executions, needs_more_steps=False)
        trace.final_response = await self._provider.generate_response(context, summary, snapshot)
        if trace.final_response.strip():
            return
        logger.warning("LLM 返回空答复，重试一次 | trace_id={}", trace.trace_id)
        trace.final_response = await self._provider.generate_response(context, summary, snapshot)

    # ── Phase 4.5：知识引用强制（M9-T09）──

    def _phase4_citation(self, trace: AgentTrace) -> None:
        """有 kb_search 结果时强制引用：无命中覆盖、引用不合规走原文摘录。

        没有 kb_search 时，拦截模型误用「知识库没有找到相关文档」话术（M9-T12）。
        """
        result = _latest_knowledge_result(trace)
        if result is None:
            self._guard_knowledge_misuse(trace)
            return
        citation = enforce_citations(trace.final_response, result)
        trace.final_response = citation.content
        trace.citations = citation.citations
        trace.citation_enforced = True
        trace.citation_no_hit = citation.no_hit
        trace.citation_fallback = citation.fallback_used
        if citation.no_hit:
            logger.info("知识库无命中，已返回固定兜底文案 | trace_id={}", trace.trace_id)

    def _guard_knowledge_misuse(self, trace: AgentTrace) -> None:
        """纯聊天未调用 kb_search，却输出无命中话术时替换为澄清提示。"""
        if KNOWLEDGE_NO_HIT_MESSAGE not in trace.final_response:
            return
        trace.final_response = KNOWLEDGE_NOT_SEARCHED_MESSAGE
        logger.warning(
            "未调用 kb_search 却输出知识库无命中话术，已替换 | trace_id={}", trace.trace_id
        )

    # ── Phase 5：幻觉检测（M3-T06；纠错闭环归 M4）──

    def _phase5_hallucination(self, trace: AgentTrace) -> None:
        """幻觉三重校验（方案支柱十）：数字事实 / 结果来源 / 业务一致性。

        只做检测（规则化、零 LLM）：命中置 ``trace.hallucination_detected``，
        详情进 warning 日志；``hallucination_resolved`` 保持 False。
        自动纠错循环暂未启用（M4-T10 决策：本轮只保证检测与拦截）。
        """
        entities = self._memory.entities() if self._memory is not None else ()
        history = self._memory.raw_messages() if self._memory is not None else ()
        result = detect_hallucinations(
            trace.final_response,
            trace.tool_executions,
            entities,
            user_input=trace.user_input,
            history=history,
        )
        trace.hallucination_detected = result.has_hallucination
        trace.hallucination_resolved = False
        if result.has_hallucination:
            logger.warning(
                "检测到幻觉: type={} confidence={} items={} hint={}",
                result.detection_type,
                result.confidence,
                len(result.hallucinated_items),
                result.correction_hint,
            )

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
        self._memory.add_raw("assistant", trace.final_response, trace_id=str(trace.trace_id))


def render_fallback_response(executions: Sequence[ToolExecution]) -> str:
    """把已执行工具结果渲染成确定性文本（零 LLM，降级路径使用）。"""
    if not executions:
        return "未执行任何查询。"

    parts: list[str] = []
    for execution in executions:
        if execution.error is not None:
            parts.append(f"工具 {execution.tool_name} 执行失败：{execution.error}")
            continue

        if execution.tool_name == KB_SEARCH_TOOL:
            knowledge = _knowledge_from_dict(execution.result)
            if knowledge is not None:
                parts.append(render_knowledge_fallback(knowledge))
                continue

        result = execution.result if isinstance(execution.result, dict) else {}
        rows = result.get("rows")
        rows = rows if isinstance(rows, list) else []
        field_keys = result.get("field_keys")
        keys = [str(key) for key in field_keys] if isinstance(field_keys, list) else []

        parts.append(f"查询 {execution.tool_name}：共 {len(rows)} 条")
        for row in rows[:_FALLBACK_MAX_ROWS]:
            if not isinstance(row, dict):
                continue
            columns = keys or list(row.keys())
            cells = [
                f"{key}={row.get(key)}"
                for key in columns
                if key in row and row.get(key) is not None
            ]
            parts.append(f"- {' | '.join(cells)}")
        if len(rows) > _FALLBACK_MAX_ROWS:
            parts.append(f"...（仅显示前 {_FALLBACK_MAX_ROWS} 条）")

    return "\n".join(parts)


def _latest_knowledge_result(trace: AgentTrace) -> KnowledgeSearchResult | None:
    """取最近一条成功的 kb_search 结果（跳过失败/坏结构）。"""
    for execution in reversed(trace.tool_executions):
        if execution.tool_name != KB_SEARCH_TOOL or execution.error is not None:
            continue
        result = _knowledge_from_dict(execution.result)
        if result is not None:
            return result
    return None


def _knowledge_from_dict(payload: object) -> KnowledgeSearchResult | None:
    """把 ToolExecution.result 还原为知识检索结果；结构不符返回 None。"""
    if not isinstance(payload, dict):
        return None
    try:
        return KnowledgeSearchResult.model_validate(payload)
    except ValidationError:
        return None
