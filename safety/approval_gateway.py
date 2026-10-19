"""审批网关协议（M9-T05）。

SafetyLayer 只依赖本协议：
- 有网关（API 模式）→ dual_approve 创建审批单
- 无网关（CLI 模式）→ 保持原来的同步双人确认
"""

from dataclasses import dataclass
from typing import Protocol

from models.llm import ToolCallDecision
from models.user import UserContext
from safety.permission_engine import PermissionDecision
from safety.risk_engine import RiskDecision


@dataclass(frozen=True)
class ApprovalRequest:
    """创建审批单所需的完整现场。"""

    trace_id: str
    session_id: str
    user: UserContext
    calls: tuple[ToolCallDecision, ...]
    risk: RiskDecision
    permission: PermissionDecision | None
    summary: str


class ApprovalGateway(Protocol):
    """审批单创建接口。"""

    async def request(self, request: ApprovalRequest) -> str:
        """创建审批单并返回 approval_id。"""
        ...
