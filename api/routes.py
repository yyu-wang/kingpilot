"""HTTP 路由（M9-T01）：health / chat / sessions。

路由层只做协议适配：
- 认证交给 ``get_current_user``
- 业务交给 ``AgentCore``
- 持久化交给 ``SessionStore``
- 错误交给 ``api.errors`` 的统一处理器
"""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from loguru import logger

from api.deps import get_current_user, get_deps
from api.errors import ApiError
from api.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    MeResponse,
    SessionListResponse,
    SessionSummary,
)
from infrastructure.exceptions import SessionStoreError
from models.base import ROLE_LABELS
from models.user import UserContext

router = APIRouter()


@router.get("/v1/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """健康检查：无认证、无环境/版本信息。"""
    return HealthResponse()


@router.get("/v1/me", response_model=MeResponse)
async def me(user: Annotated[UserContext, Depends(get_current_user)]) -> MeResponse:
    """当前登录账号的身份与角色（控制台顶栏展示）。"""
    return MeResponse(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        role=user.role,
        role_label=ROLE_LABELS.get(user.role, user.role),
    )


@router.post("/v1/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    http_response: Response,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> ChatResponse:
    """单轮/多轮对话：认证 → 加载会话 → 主循环 → 落盘。"""
    deps = get_deps(request)
    message = payload.message.strip()
    if not message:
        raise ApiError(422, "empty_message", "消息不能为空")

    session_id = payload.session_id or uuid4().hex[:12]
    store = deps.store_for(user.user_id, tenant_id=user.tenant_id)

    # 读-改-写整体加锁：同会话并发请求不能互相覆盖
    async with deps.locks.get(f"{user.tenant_id}:{user.user_id}:{session_id}"):
        try:
            memory = store.load(session_id)
        except SessionStoreError as exc:
            raise ApiError(400, "session_invalid", "会话不存在或已损坏") from exc

        response = await deps.build_core(memory, user=user).run(message, user_context=user)

        try:
            store.save(memory)
        except SessionStoreError as exc:
            raise ApiError(500, "session_write_failed", "会话保存失败，请重试") from exc

        sampler = deps.resources_for(user.tenant_id).sampler
        if sampler is not None:
            try:
                sampler.maybe_sample(response, session_id=session_id)
            except Exception as exc:  # 旁路失败不影响主响应
                logger.warning("在线抽样失败（不影响响应）: {}", exc)

    if response.approval_pending:
        # 写操作已提交审批但尚未执行：202 而不是 200
        http_response.status_code = 202

    return ChatResponse(
        session_id=session_id,
        trace_id=str(response.trace_id),
        content=response.content,
        blocked=response.blocked,
        cancelled=response.cancelled,
        hallucination_warning=response.hallucination_warning,
        approval_pending=response.approval_pending,
        approval_id=response.approval_id,
    )


@router.get("/v1/sessions", response_model=SessionListResponse)
async def list_sessions(
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> SessionListResponse:
    """列出当前用户自己的会话（目录级隔离）。"""
    infos = get_deps(request).store_for(user.user_id, tenant_id=user.tenant_id).list_sessions()
    return SessionListResponse(
        sessions=[
            SessionSummary(
                session_id=info.session_id,
                rounds=info.rounds,
                updated_at=info.updated_at,
                title=info.title,
            )
            for info in infos
        ]
    )


@router.get("/v1/sessions/{session_id}", response_model=SessionSummary)
async def get_session(
    session_id: str,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> SessionSummary:
    """单个会话元信息；不存在或不属于当前用户一律 404。"""
    store = get_deps(request).store_for(user.user_id, tenant_id=user.tenant_id)
    try:
        store.path_for(session_id)  # ID 白名单校验（防路径穿越）
    except SessionStoreError as exc:
        raise ApiError(400, "session_invalid", "会话 ID 非法") from exc

    for info in store.list_sessions():
        if info.session_id == session_id:
            return SessionSummary(
                session_id=info.session_id,
                rounds=info.rounds,
                updated_at=info.updated_at,
                title=info.title,
            )
    raise ApiError(404, "session_not_found", "会话不存在")
