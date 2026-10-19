"""API 边界模型（M9-T01）。

只定义 HTTP 请求/响应形状；业务对象仍复用 ``models.*``，不在本层重复建模。
统一继承 ``KingPilotBaseModel``：``extra="forbid"``，未知字段直接 422。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from models.approval import ApprovalDecisionType, ApprovalStatus
from models.base import KingPilotBaseModel


class ChatRequest(KingPilotBaseModel):
    """对话请求。``session_id`` 为空时由服务端生成。"""

    message: str = Field(min_length=1, max_length=4000, description="用户消息")
    session_id: str = Field(default="", max_length=64, description="会话 ID（可选）")


class ChatResponse(KingPilotBaseModel):
    """对话响应（不返回完整 Trace，避免把工具原始结果带出边界）。"""

    session_id: str = Field(description="会话 ID")
    trace_id: str = Field(description="本次请求的 Trace ID")
    content: str = Field(description="最终答复")
    blocked: bool = Field(default=False, description="是否被安全护栏/DLP 拦截")
    cancelled: bool = Field(default=False, description="是否被 HITL 确认取消")
    hallucination_warning: bool = Field(default=False, description="是否存在幻觉告警")


class SessionSummary(KingPilotBaseModel):
    """会话元信息（不返回对话正文）。"""

    session_id: str = Field(description="会话 ID")
    rounds: int = Field(description="对话轮数")
    updated_at: str = Field(description="最近保存时间")


class SessionListResponse(KingPilotBaseModel):
    """当前用户的会话列表。"""

    sessions: list[SessionSummary] = Field(default_factory=list)


class MessageView(KingPilotBaseModel):
    """会话历史中的一条消息（只含对话文本，不含工具原始结果）。"""

    role: Literal["user", "assistant"]
    content: str


class SessionMessagesResponse(KingPilotBaseModel):
    """会话历史响应。"""

    session_id: str
    messages: list[MessageView] = Field(default_factory=list)


class TraceSummary(KingPilotBaseModel):
    """Trace 列表条目。"""

    trace_id: str
    session_id: str = ""
    user_input: str = ""
    used_fallback: bool = False
    total_latency_ms: float = 0.0
    saved_at: str = ""


class TraceListResponse(KingPilotBaseModel):
    """当前用户的 Trace 列表。"""

    traces: list[TraceSummary] = Field(default_factory=list)


class TraceView(KingPilotBaseModel):
    """单条 Trace 的脱敏视图（不返回原始 tool_executions）。"""

    trace_id: str
    session_id: str = ""
    user_id: str = ""
    user_input: str = ""
    final_response: str = ""
    report_text: str = ""


class ApprovalDecisionView(KingPilotBaseModel):
    """一条审批意见。"""

    approver_id: str
    decision: ApprovalDecisionType
    comment: str = ""
    decided_at: datetime


class ApprovalView(KingPilotBaseModel):
    """审批单视图（不包含 arguments / idempotency_key）。"""

    approval_id: str
    trace_id: str
    session_id: str = ""
    requester_id: str
    requester_role: str
    tool_name: str
    summary: str = ""
    form_id: str = ""
    business_key: str = ""
    risk_score: float = 0.0
    risk_level: str = ""
    required_approvers: list[str] = Field(default_factory=list)
    required_approvals: int
    status: ApprovalStatus
    decisions: list[ApprovalDecisionView] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    can_decide: bool = False


class ApprovalListResponse(KingPilotBaseModel):
    """审批单列表。"""

    approvals: list[ApprovalView] = Field(default_factory=list)


class ApprovalActionRequest(KingPilotBaseModel):
    """通过/拒绝请求体。"""

    comment: str = Field(default="", max_length=500, description="审批意见（可空）")


class HealthResponse(KingPilotBaseModel):
    """健康检查响应（不含环境/版本等可被侦察的信息）。"""

    status: Literal["ok"] = "ok"


class ErrorBody(KingPilotBaseModel):
    """错误信封主体。"""

    code: str = Field(description="稳定错误码（前端按此分支）")
    message: str = Field(description="可展示给用户的说明")


class ErrorEnvelope(KingPilotBaseModel):
    """所有非 2xx 响应的统一形状。"""

    error: ErrorBody
