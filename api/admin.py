"""管理后台 API：订单变更审计 + 测试数据集浏览（admin-only）。

只读接口：
- ``GET /v1/admin/overview``：账本统计 + 各数据集行数
- ``GET /v1/admin/changes``：写操作变更记录（含 before/after 快照与 diff）
- ``GET /v1/admin/datasets/{kind}``：当前业务数据（销售/采购/入库/基础资料/库存）

权限：仅 ``admin``。管理后台可看到全量单据与操作人，不向 manager/viewer 开放。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from api.deps import get_current_user, get_deps
from api.errors import ApiError
from governance.admin_console import DATASETS, AdminConsoleService
from models.admin import AdminOverview, ChangeListResponse, DatasetResponse
from models.user import UserContext

router = APIRouter()


def _service(request: Request, user: UserContext) -> AdminConsoleService:
    deps = get_deps(request)
    return AdminConsoleService(deps.client, deps.resources_for(user.tenant_id).ledger)


def _require_admin(user: UserContext) -> None:
    if user.role != "admin":
        raise ApiError(403, "forbidden", "仅管理员可访问管理后台")


@router.get("/v1/admin/overview", response_model=AdminOverview)
async def overview(
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> AdminOverview:
    """管理后台总览。"""
    _require_admin(user)
    return await _service(request, user).overview()


@router.get("/v1/admin/changes", response_model=ChangeListResponse)
async def changes(
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
    form_id: str = "",
    action: str = "",
    status: str = "",
    q: str = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> ChangeListResponse:
    """写操作变更记录（时间倒序）。"""
    _require_admin(user)
    return _service(request, user).changes(
        form_id=form_id,
        action=action,
        status=status,
        query=q,
        limit=limit,
    )


@router.get("/v1/admin/datasets/{kind}", response_model=DatasetResponse)
async def dataset(
    kind: str,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
    q: str = "",
    limit: Annotated[int, Query(ge=1, le=2000)] = 2000,
) -> DatasetResponse:
    """按数据集类型返回当前业务数据。"""
    _require_admin(user)
    if kind not in DATASETS:
        raise ApiError(404, "dataset_not_found", "数据集不存在")
    return await _service(request, user).dataset(kind, query=q, limit=limit)
