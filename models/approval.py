"""审批单模型（M9-T04）。

一张审批单 = 一次高风险写操作的待确认现场：
- 状态机：pending → approved / rejected / expired
- 审批链：decisions 按时间追加，每人只允许一票
- arguments 仅供 T05 恢复执行，API 不返回
"""

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from models.base import KingPilotBaseModel, Role
from models.ledger import WriteAction

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]
ApprovalDecisionType = Literal["approve", "reject"]
ApprovalScope = Literal["inbox", "mine", "all"]
ApprovalExecutionStatus = Literal["not_started", "running", "succeeded", "failed"]


class ApprovalDecision(KingPilotBaseModel):
    """一条审批意见（追加后不再修改）。"""

    approver_id: str = Field(min_length=1, max_length=64, description="审批人 user_id")
    decision: ApprovalDecisionType = Field(description="通过 / 拒绝")
    comment: str = Field(default="", max_length=500, description="审批意见（已脱敏）")
    decided_at: datetime = Field(description="决策时间（UTC）")


class PendingAction(KingPilotBaseModel):
    """一张待审批动作单。"""

    approval_id: str = Field(
        default_factory=lambda: uuid4().hex,
        description="审批单 ID（同时作为文件名）",
    )
    trace_id: str = Field(description="发起请求的 Trace ID")
    session_id: str = Field(default="", description="会话 ID")
    requester_id: str = Field(min_length=1, max_length=64, description="发起人")
    requester_role: Role = Field(description="发起人角色")
    tool_name: str = Field(min_length=1, description="待执行的写工具")
    summary: str = Field(default="", max_length=2000, description="脱敏后的动作摘要")
    form_id: str = Field(default="", description="金蝶 FormId")
    business_key: str = Field(default="", description="业务单号")
    risk_score: float = Field(default=0.0, ge=0.0, le=100.0)
    risk_level: str = Field(default="", description="风险档位")
    required_approvers: list[str] = Field(
        default_factory=list, description="风险/权限建议的审批角色（展示用）"
    )
    required_approvals: int = Field(default=2, ge=1, description="所需通过票数")
    idempotency_key: str = Field(min_length=1, max_length=128, description="创建幂等键")
    status: ApprovalStatus = Field(default="pending", description="审批状态")
    decisions: list[ApprovalDecision] = Field(default_factory=list)
    created_at: datetime = Field(description="创建时间（UTC）")
    updated_at: datetime = Field(description="更新时间（UTC）")
    expires_at: datetime = Field(description="过期时间（UTC）")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="待执行参数（仅供 T05，API 不返回）"
    )

    # ── 执行状态（M9-T05）──
    write_action: WriteAction = Field(default="save", description="写动作")
    execution_status: ApprovalExecutionStatus = Field(default="not_started", description="执行状态")
    execution_error: str = Field(default="", max_length=500, description="安全错误摘要")
    execution_result: dict[str, Any] = Field(
        default_factory=dict, description="执行结果摘要（不含原始报文）"
    )
    executed_at: datetime | None = Field(default=None, description="执行完成时间（UTC）")
