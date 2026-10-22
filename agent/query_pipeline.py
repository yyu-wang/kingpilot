"""单轮查询主链路（M2-T04）：NL → plan → 执行 → 回填 → 答复。

方案 §2.3 主循环的 M2 版边界（任务卡 D1）：
- 覆盖 Phase 1（plan）+ Phase 3（执行）+ Phase 4（答复）；Phase 0（用户体系
  组装）与 reflect 多步编排（Plan-and-Solve）归 M3——本管道是多步主循环的内核雏形，
  M3 直接复用而非重写。
- Guard 1-5 未接入（M4）：形状级防线已在 ``ToolDefinition.execute``
  （结构化参数 fail-closed + 端点白名单组装），AST 校验就位后插入执行之前。
- 错误不中断（D2）：工具失败记入 ``ToolExecution.error`` 继续走完链路，
  LLM 拿着错误上下文向用户解释；``LLMUnavailableError`` 直接传播——
  降级是主循环（M3）的职责，本层不吞。
"""

import time
from typing import Any

from agent.llm import LLMProvider
from agent.tools import ToolRegistry
from erp.protocol import ErpClient
from infrastructure.exceptions import KingPilotError
from models.agent import AgentResponse, AgentTrace
from models.llm import ExecutionSummary, LLMContext, LLMMessage, MemorySnapshot
from models.tool import ToolExecution
from models.user import UserContext


def _default_user_context() -> UserContext:
    """本地开发默认用户：最小权限角色（viewer 只读，查询场景合理默认）。

    正式的用户体系与四层权限隔离是 M3/M5 的 Phase 0 职责，本工厂只为
    M2 链路提供 AgentTrace 必填字段的合法占位。
    """
    return UserContext(user_id="local_dev", role="viewer")


class QueryPipeline:
    """M2 查询管道：一次 run = plan → 工具执行 → 结果回填 → 自然语言答复。"""

    def __init__(self, provider: LLMProvider, tools: ToolRegistry, client: ErpClient) -> None:
        self._provider = provider
        self._tools = tools
        self._client = client

    async def run(self, user_input: str, user_context: UserContext | None = None) -> AgentResponse:
        """执行单轮查询链路，返回带完整 trace 的响应。

        Args:
            user_input: 用户自然语言输入。
            user_context: 用户上下文；缺省用本地开发最小权限用户。

        Returns:
            AgentResponse：``content`` 为最终答复，``trace`` 为全链路记录
            （工具执行明细/规划记录/耗时，审计与 M8 评测的数据源）。

        Raises:
            LLMUnavailableError: LLM 不可用（本层不降级，传播给 M3 主循环处理）。
        """
        started = time.perf_counter()
        context_for_user = user_context or _default_user_context()
        trace = AgentTrace(
            user_input=user_input,
            user_context=context_for_user,
            user_id=context_for_user.user_id,  # 审计冗余字段（规范：便于按用户查询）
        )
        context = LLMContext(messages=[LLMMessage(role="user", content=user_input)])

        # ① plan：全部工具声明给 LLM（可见性治理——写工具就位后按角色过滤）
        plan = await self._provider.plan(context, tools=self._tools.all_specs())
        trace.llm_planning = {
            "reasoning": plan.reasoning,
            "tool_calls": [decision.model_dump() for decision in plan.tool_calls],
        }

        # ②③ 执行：失败不中断（D2），错误逐条记录进 ToolExecution
        for decision in plan.tool_calls:
            trace.tool_executions.append(
                await self._execute_one(decision.tool_name, decision.arguments)
            )

        # ④ 回填答复：ExecutionSummary 携带成功结果与失败原因，LLM 据此作答
        summary = ExecutionSummary(tool_executions=trace.tool_executions, needs_more_steps=False)
        trace.final_response = await self._provider.generate_response(
            context, summary, MemorySnapshot()
        )
        trace.total_latency_ms = (time.perf_counter() - started) * 1000
        return AgentResponse(content=trace.final_response, trace_id=trace.trace_id, trace=trace)

    async def _execute_one(self, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        """执行单个工具调用并归一为执行记录（成功带 result，失败带 error）。

        Why 只捕 KingPilotError：项目异常层次保证其消息不含密钥（红线 3.6）；
        其他异常（编程错误）不吞，直接暴露。参数脱敏归 M7，本轮查询参数非敏感。
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
