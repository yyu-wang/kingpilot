"""审批网关实现（M9-T05）：把 HITL 双人复核落成审批单。

边界：
- 只支持单个写调用；多调用 fail-closed
- 工具必须是 write/action 类型，且 action 属于六种写动作
- 幂等键 = trace_id + M5 canonical key，同一请求重试不重复建单
"""

from typing import cast

from agent.tools import ToolRegistry
from governance.approval import ApprovalService
from infrastructure.exceptions import ApprovalError, ToolError
from kingdee.idempotency import build_idempotency_key
from models.ledger import WriteAction
from safety.approval_gateway import ApprovalRequest
from safety.permission_engine import PermissionDecision
from safety.risk_engine import RiskDecision

_WRITE_ACTIONS: frozenset[str] = frozenset({"save", "edit", "submit", "audit", "unaudit", "delete"})
_BUSINESS_KEY_CANDIDATES = (
    "bill_no",
    "business_key",
    "order_no",
    "document_number",
    "number",
)


class ApprovalGatewayImpl:
    """基于 ApprovalService 的网关实现。"""

    def __init__(self, approvals: ApprovalService, tools: ToolRegistry) -> None:
        self._approvals = approvals
        self._tools = tools

    async def request(self, request: ApprovalRequest) -> str:
        if len(request.calls) != 1:
            raise ApprovalError("一次审批仅支持单个写操作", code="approval_conflict")
        call = request.calls[0]
        try:
            tool = self._tools.get(call.tool_name)
        except ToolError as exc:
            raise ApprovalError("审批工具不存在", code="approval_conflict") from exc
        if tool.action not in _WRITE_ACTIONS:
            raise ApprovalError("审批工具不是写操作", code="approval_conflict")

        digest = build_idempotency_key(request.user.user_id, call.tool_name, call.arguments)
        approval = await self._approvals.create(
            trace_id=request.trace_id,
            session_id=request.session_id,
            requester=request.user,
            tool_name=call.tool_name,
            arguments=call.arguments,
            summary=request.summary,
            form_id=tool.endpoint.form_id,
            business_key=_business_key(call.arguments),
            risk_score=request.risk.score,
            risk_level=request.risk.level,
            required_approvers=_merge_approvers(request.risk, request.permission),
            idempotency_key=f"{request.trace_id}:{digest}",
            write_action=cast(WriteAction, tool.action),
        )
        return approval.approval_id


def _business_key(arguments: dict[str, object]) -> str:
    for key in _BUSINESS_KEY_CANDIDATES:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _merge_approvers(risk: RiskDecision, permission: PermissionDecision | None) -> tuple[str, ...]:
    approvers = list(risk.required_approvers)
    if permission is not None:
        approvers.extend(permission.required_approvers)
    return tuple(dict.fromkeys(approvers))
