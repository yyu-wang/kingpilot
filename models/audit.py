"""审计事件模型（M6-T04，方案 §5.2 支柱七）。

统一审计流字段：who / when / what / risk / result。
M4 拦截、M5 账本、M6 告警都写同一模型，便于后续审计查询与合规留存。
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from models.base import KingPilotBaseModel

AuditEventType = Literal[
    "guard.blocked",
    "permission.denied",
    "ledger.write",
    "saga.failed",
    "alert.fired",
    "dlp.blocked",
    "budget.downgraded",
    "budget.exhausted",
    "approval.requested",
    "approval.approved",
    "approval.rejected",
    "approval.expired",
    "approval.execution_started",
    "approval.executed",
    "approval.execution_failed",
    "cost.recorded",
    "badcase.marked",
    "badcase.exported",
    "rag.indexed",
]

AuditStatus = Literal["success", "failed", "blocked", "rejected", "rolled_back"]


class AuditEvent(KingPilotBaseModel):
    """一条不可变审计事件。"""

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: AuditEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    trace_id: str = ""
    user_id: str = ""
    user_role: str = ""
    ip_address: str = ""

    action: str = ""
    form_id: str = ""
    business_key: str = ""
    tool_name: str = ""
    # 默认不落原始参数（防密钥/敏感值入审计）；需要时由调用方显式脱敏后传入
    tool_call_parameters: dict[str, Any] = Field(default_factory=dict)

    risk_score: float = 0.0
    risk_level: str = ""
    approval_chain: list[str] = Field(default_factory=list)

    status: AuditStatus = "success"
    saga_id: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return self.model_dump(mode="json")
