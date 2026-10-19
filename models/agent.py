"""Agent 核心类型（M0-T06）。

方案 §2.3 主循环的输入/输出/全链路追踪。
字段与伪代码 + 审计日志定义严格对齐。
"""

from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from models.base import KingPilotBaseModel
from models.tool import ToolExecution
from models.user import UserContext


class AgentTrace(KingPilotBaseModel):
    """全链路追踪（方案 §2.3 + 审计日志）。

    一次用户请求的完整生命周期记录，贯穿 Phase 0~6。
    """

    # ── 核心字段（方案 §2.3 直接引用）──
    trace_id: UUID = Field(default_factory=uuid4, description="追踪 ID（UUID 保证唯一性）")
    user_input: str = Field(description="用户原始输入")
    user_context: UserContext = Field(description="用户上下文（四层权限隔离）")
    tool_executions: list[ToolExecution] = Field(
        default_factory=list, description="工具执行记录列表（Phase 3）"
    )
    reflections: list[dict[str, Any]] = Field(
        default_factory=list, description="反思记录（Phase 4，Plan-and-Solve 多步编排）"
    )
    final_response: str = Field(default="", description="最终响应（Phase 4/5）")

    # ── 审计扩展字段（审计日志需要）──
    user_id: str = Field(description="用户 ID（冗余自 user_context，便于审计查询）")
    session_id: str = Field(default="", description="会话 ID（多轮对话聚合）")
    approval_id: str = Field(default="", description="审批单 ID（M9-T05 审批待处理时）")
    llm_planning: dict[str, Any] = Field(
        default_factory=dict, description="LLM 规划记录（Phase 1，含降级标志）"
    )
    guard_checks: list[dict[str, Any]] = Field(
        default_factory=list, description="安全护栏检查记录（Phase 2，Guard 1-5）"
    )
    risk_assessment: dict[str, Any] = Field(
        default_factory=dict, description="风险评估结果（Phase 2）"
    )
    permission_check: dict[str, Any] = Field(
        default_factory=dict, description="权限检查结果（Phase 2，四层权限）"
    )
    saga_transaction: dict[str, Any] = Field(
        default_factory=dict, description="Saga 事务记录（Phase 3）"
    )
    total_latency_ms: float = Field(default=0.0, description="总耗时（毫秒）")
    llm_token_usage: dict[str, int] = Field(
        default_factory=dict, description="LLM Token 用量（prompt/completion/total）"
    )
    kingdee_api_calls: int = Field(default=0, description="金蝶 API 调用次数")

    # ── 状态标志（主循环分支用）──
    used_fallback: bool = Field(default=False, description="是否使用了降级（LLM 不可用时）")
    hallucination_detected: bool = Field(default=False, description="是否检测到幻觉（Phase 5）")
    hallucination_resolved: bool = Field(
        default=False, description="幻觉是否已通过纠错循环解决（Phase 5）"
    )

    # ── 知识引用（M9-T09）──
    citations: list[str] = Field(default_factory=list, description="命中分块的引用列表")
    citation_enforced: bool = Field(default=False, description="是否执行过引用强制校验")
    citation_no_hit: bool = Field(default=False, description="知识库是否无命中")
    citation_fallback: bool = Field(
        default=False, description="LLM 引用不合规时是否走了确定性摘录兜底"
    )


class AgentResponse(KingPilotBaseModel):
    """主循环出口（方案 §2.3）。"""

    content: str = Field(default="", description="最终响应内容（Phase 4/5）")
    trace_id: UUID = Field(description="追踪 ID（与 AgentTrace.trace_id 一致）")
    blocked: bool = Field(default=False, description="是否被安全护栏拦截（Phase 2）")
    reason: str = Field(default="", description="拦截原因（blocked=True 时）")
    cancelled: bool = Field(default=False, description="是否被用户取消（HITL 确认拒绝）")
    saga_status: str = Field(default="", description="Saga 事务状态（Phase 3）")
    hallucination_warning: bool = Field(default=False, description="幻觉警告（检测到但未解决）")
    approval_pending: bool = Field(
        default=False, description="是否进入审批待处理（M9-T05，写操作未执行）"
    )
    approval_id: str = Field(default="", description="审批单 ID（approval_pending=True 时）")
    trace: AgentTrace | None = Field(default=None, description="完整追踪记录（可选返回）")
