"""审批中心 API（M9-T04）：列表 / 详情 / 通过 / 拒绝。

创建入口不在本卡：T05 的 HITL 网关会调用 ``ApprovalService.create``。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from api.deps import get_current_user, get_deps
from api.schemas import (
    ApprovalActionRequest,
    ApprovalDecisionView,
    ApprovalListResponse,
    ApprovalView,
)
from config.approval_rules import APPROVER_ROLES
from infrastructure.exceptions import ApprovalError
from models.approval import ApprovalScope, ApprovalStatus, PendingAction
from models.user import UserContext

router = APIRouter()


def _to_view(action: PendingAction, user: UserContext) -> ApprovalView:
    """领域对象 → API 视图；不暴露 arguments/idempotency_key。"""
    decided_ids = {decision.approver_id for decision in action.decisions}
    can_decide = (
        action.status == "pending"
        and user.role in APPROVER_ROLES
        and user.user_id != action.requester_id
        and user.user_id not in decided_ids
    )
    return ApprovalView(
        approval_id=action.approval_id,
        trace_id=action.trace_id,
        session_id=action.session_id,
        requester_id=action.requester_id,
        requester_role=str(action.requester_role),
        tool_name=action.tool_name,
        summary=action.summary,
        form_id=action.form_id,
        business_key=action.business_key,
        risk_score=action.risk_score,
        risk_level=action.risk_level,
        required_approvers=action.required_approvers,
        required_approvals=action.required_approvals,
        status=action.status,
        decisions=[
            ApprovalDecisionView(
                approver_id=decision.approver_id,
                decision=decision.decision,
                comment=decision.comment,
                decided_at=decision.decided_at,
            )
            for decision in action.decisions
        ],
        created_at=action.created_at,
        updated_at=action.updated_at,
        expires_at=action.expires_at,
        can_decide=can_decide,
        execution_status=action.execution_status,
        execution_error=action.execution_error,
        executed_at=action.executed_at,
    )


@router.get("/v1/approvals", response_model=ApprovalListResponse)
async def list_approvals(
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
    scope: ApprovalScope = "inbox",
    status: ApprovalStatus | None = None,
    limit: int = 50,
) -> ApprovalListResponse:
    """按可见范围列出审批单（默认 inbox）。"""
    actions = await get_deps(request).approvals.list_for_user(
        user, scope=scope, status=status, limit=limit
    )
    return ApprovalListResponse(approvals=[_to_view(action, user) for action in actions])


@router.get("/v1/approvals/{approval_id}", response_model=ApprovalView)
async def get_approval(
    approval_id: str,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> ApprovalView:
    """审批单详情；非发起人且非审批角色 → 404。"""
    action = await get_deps(request).approvals.get(approval_id, user)
    return _to_view(action, user)


@router.post("/v1/approvals/{approval_id}/approve", response_model=ApprovalView)
async def approve(
    approval_id: str,
    payload: ApprovalActionRequest,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> ApprovalView:
    """投一张通过票；两票（不同用户）后状态变 approved。"""
    deps = get_deps(request)
    action = await deps.approvals.approve(approval_id, user, payload.comment)
    if action.status == "approved":
        # 双人复核完成的同一请求内恢复执行；失败只落 execution_status
        action = await deps.approval_execution.execute(action.approval_id)
    return _to_view(action, user)


@router.post("/v1/approvals/{approval_id}/reject", response_model=ApprovalView)
async def reject(
    approval_id: str,
    payload: ApprovalActionRequest,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> ApprovalView:
    """拒绝审批单（拒绝优先）。"""
    action = await get_deps(request).approvals.reject(approval_id, user, payload.comment)
    return _to_view(action, user)


@router.post("/v1/approvals/{approval_id}/execute", response_model=ApprovalView)
async def execute(
    approval_id: str,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> ApprovalView:
    """手动恢复执行（admin/manager）；用于进程重启后的 approved 单。"""
    if user.role not in APPROVER_ROLES:
        raise ApprovalError("当前角色无执行权限", code="approval_forbidden")
    action = await get_deps(request).approval_execution.execute(approval_id)
    return _to_view(action, user)
