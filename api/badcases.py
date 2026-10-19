"""Badcase 处理台 API（M9-T07）：标记 / 列表 / 详情 / 导出。

权限：只允许 admin/manager；评测集是发布门禁输入，普通用户不能污染。
边界：响应只返回脱敏后的问题与人工说明，不返回 Trace、工具结果或最终答复。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from api.deps import get_current_user, get_deps
from api.errors import ApiError
from api.schemas import (
    BadcaseExportRequest,
    BadcaseListResponse,
    BadcaseMarkRequest,
    BadcaseView,
)
from config.badcase_rules import BADCASE_REVIEWER_ROLES
from models.badcase import BadcaseRecord, BadcaseStatus
from models.user import UserContext

router = APIRouter()


def _require_reviewer(user: UserContext) -> None:
    if user.role not in BADCASE_REVIEWER_ROLES:
        raise ApiError(403, "forbidden", "当前角色无 Badcase 操作权限")


def _to_view(record: BadcaseRecord) -> BadcaseView:
    return BadcaseView(
        badcase_id=record.badcase_id,
        trace_id=record.trace_id,
        question=record.question,
        expected_tool=record.expected_tool,
        expected_behavior=record.expected_behavior,
        category=record.category,
        reason=record.reason,
        status=record.status,
        created_by=record.created_by,
        created_at=record.created_at,
        exported_dataset=record.exported_dataset,
        exported_question_id=record.exported_question_id,
        exported_at=record.exported_at,
    )


@router.post("/v1/badcases", response_model=BadcaseView)
async def mark_badcase(
    payload: BadcaseMarkRequest,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> BadcaseView:
    """从 Trace 标记一条 Badcase；同一 Trace 幂等返回原记录。"""
    _require_reviewer(user)
    record = (
        await get_deps(request)
        .resources_for(user.tenant_id)
        .badcases.mark(
            trace_id=payload.trace_id,
            expected_tool=payload.expected_tool,
            reason=payload.reason,
            category=payload.category,
            expected_behavior=payload.expected_behavior,
            user=user,
        )
    )
    return _to_view(record)


@router.get("/v1/badcases", response_model=BadcaseListResponse)
async def list_badcases(
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
    status: BadcaseStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> BadcaseListResponse:
    """按创建时间倒序列出 Badcase。"""
    _require_reviewer(user)
    records = (
        await get_deps(request)
        .resources_for(user.tenant_id)
        .badcases.list_records(user, status=status, limit=limit)
    )
    return BadcaseListResponse(badcases=[_to_view(record) for record in records])


@router.get("/v1/badcases/{badcase_id}", response_model=BadcaseView)
async def get_badcase(
    badcase_id: str,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> BadcaseView:
    """Badcase 详情；不存在 → 404。"""
    _require_reviewer(user)
    record = await get_deps(request).resources_for(user.tenant_id).badcases.get(badcase_id, user)
    return _to_view(record)


@router.post("/v1/badcases/{badcase_id}/export", response_model=BadcaseView)
async def export_badcase(
    badcase_id: str,
    payload: BadcaseExportRequest,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> BadcaseView:
    """导出到版本化评测集；重复导出幂等，重复用例 → 409。"""
    _require_reviewer(user)
    record = (
        await get_deps(request)
        .resources_for(user.tenant_id)
        .badcases.export(
            badcase_id,
            dataset_version=payload.dataset_version,
            user=user,
        )
    )
    return _to_view(record)
