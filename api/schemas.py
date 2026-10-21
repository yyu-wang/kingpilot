"""API 边界模型（M9-T01）。

只定义 HTTP 请求/响应形状；业务对象仍复用 ``models.*``，不在本层重复建模。
统一继承 ``KingPilotBaseModel``：``extra="forbid"``，未知字段直接 422。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from models.approval import ApprovalDecisionType, ApprovalStatus
from models.badcase import BadcaseStatus
from models.base import KingPilotBaseModel, Role


class MeResponse(KingPilotBaseModel):
    """当前登录账号（控制台顶栏展示用；不含任何密钥信息）。"""

    user_id: str = Field(description="账号标识")
    tenant_id: str = Field(description="所属租户")
    role: Role = Field(description="角色（英文枚举值，鉴权口径）")
    role_label: str = Field(description="角色中文展示名")


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
    approval_pending: bool = Field(default=False, description="是否进入审批待处理（HTTP 202）")
    approval_id: str = Field(default="", description="审批单 ID")


class SessionSummary(KingPilotBaseModel):
    """会话元信息（不返回对话正文）。"""

    session_id: str = Field(description="会话 ID")
    rounds: int = Field(description="对话轮数")
    updated_at: str = Field(description="最近保存时间")
    title: str = Field(default="", description="会话标题（首条提问截断）")


class SessionListResponse(KingPilotBaseModel):
    """当前用户的会话列表。"""

    sessions: list[SessionSummary] = Field(default_factory=list)


class MessageView(KingPilotBaseModel):
    """会话历史中的一条消息（只含对话文本，不含工具原始结果）。"""

    role: Literal["user", "assistant"]
    content: str
    trace_id: str = ""  # assistant 消息可回看对应 Trace；旧会话为空


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
    requester_role_label: str = ""
    tool_name: str
    tool_label: str = ""
    summary: str = ""
    form_id: str = ""
    business_key: str = ""
    risk_score: float = 0.0
    risk_level: str = ""
    risk_level_label: str = ""
    required_approvers: list[str] = Field(default_factory=list)
    required_approvals: int
    status: ApprovalStatus
    decisions: list[ApprovalDecisionView] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    can_decide: bool = False
    execution_status: str = "not_started"
    execution_error: str = ""
    executed_at: datetime | None = None


class ApprovalListResponse(KingPilotBaseModel):
    """审批单列表。"""

    approvals: list[ApprovalView] = Field(default_factory=list)


class ApprovalActionRequest(KingPilotBaseModel):
    """通过/拒绝请求体。"""

    comment: str = Field(default="", max_length=500, description="审批意见（可空）")


class BadcaseMarkRequest(KingPilotBaseModel):
    """标记 Badcase 请求体。

    ``expected_tool`` 必填且可为 null：null 表示期望纯聊天、不调用工具；
    不提供默认值，避免“漏填被当成纯聊天”污染评测集。
    """

    trace_id: str = Field(min_length=1, max_length=64, description="来源 Trace ID")
    expected_tool: str | None = Field(description="期望工具；null 表示期望纯聊天")
    reason: str = Field(description="标记原因（1~500 字符）")
    category: str = Field(default="badcase", description="分类（人工可读）")
    expected_behavior: str = Field(default="", description="期望行为说明（可选）")


class BadcaseExportRequest(KingPilotBaseModel):
    """导出到评测集请求体。"""

    dataset_version: str = Field(default="v1", description="目标数据集版本")


class BadcaseView(KingPilotBaseModel):
    """Badcase 视图（只含脱敏后的问题与人工说明）。"""

    badcase_id: str
    trace_id: str
    question: str
    expected_tool: str | None
    expected_behavior: str = ""
    category: str
    reason: str
    status: BadcaseStatus
    created_by: str
    created_at: datetime
    exported_dataset: str = ""
    exported_question_id: str = ""
    exported_at: datetime | None = None


class BadcaseListResponse(KingPilotBaseModel):
    """Badcase 列表。"""

    badcases: list[BadcaseView] = Field(default_factory=list)


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
