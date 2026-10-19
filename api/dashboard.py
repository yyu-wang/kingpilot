"""运营看板 API（M9-T06）：admin-only 聚合查询。"""

from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request

from api.deps import get_current_user, get_deps
from api.errors import ApiError
from models.dashboard import DashboardOverview
from models.user import UserContext

router = APIRouter()

_DEFAULT_DAYS = 7


@router.get("/v1/dashboard/overview", response_model=DashboardOverview)
async def overview(
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> DashboardOverview:
    """运营看板总览；仅 admin 可访问。"""
    if user.role != "admin":
        raise ApiError(403, "forbidden", "仅管理员可查看看板")

    deps = get_deps(request)
    local_tz = ZoneInfo(deps.settings.dashboard_timezone)
    today = datetime.now(local_tz).date()
    resolved_to = to_date or today
    resolved_from = from_date or (resolved_to - timedelta(days=_DEFAULT_DAYS - 1))
    try:
        return deps.resources_for(user.tenant_id).dashboard.overview(resolved_from, resolved_to)
    except ValueError as exc:
        raise ApiError(422, "invalid_range", str(exc)) from exc
