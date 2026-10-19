"""控制台只读接口（M9-T03）：会话历史 + Trace 列表/详情。

安全边界：
- 会话/ Trace 均按当前用户归属过滤，越权统一 404（不暴露存在性）
- 历史只返回 user/assistant 文本，工具原始结果不进接口
- Trace 只返回脱敏后的报告文本，不返回原始 tool_executions
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from api.deps import get_current_user, get_deps
from api.errors import ApiError
from api.schemas import (
    MessageView,
    SessionMessagesResponse,
    TraceListResponse,
    TraceSummary,
    TraceView,
)
from infrastructure.exceptions import SessionStoreError, TraceStoreError
from models.user import UserContext
from observability.tracing import render_trace_report
from safety.datasecurity import redact_text
from safety.dlp import DlpScanner

router = APIRouter()

_MAX_TRACE_INPUT_CHARS = 120
_MAX_TRACE_LIMIT = 200
_BLOCKED_CONTENT = "（该条内容包含敏感信息，已阻断展示）"
_BLOCKED_REPORT = "（Trace 包含敏感信息，已阻断展示）"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}..."


@router.get("/v1/sessions/{session_id}/messages", response_model=SessionMessagesResponse)
async def session_messages(
    session_id: str,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> SessionMessagesResponse:
    """当前用户的会话历史（只含 user/assistant，统一脱敏）。"""
    store = get_deps(request).store_for(user.user_id, tenant_id=user.tenant_id)
    try:
        path = store.path_for(session_id)
    except SessionStoreError as exc:
        raise ApiError(400, "session_invalid", "会话 ID 非法") from exc
    if not path.exists():
        raise ApiError(404, "session_not_found", "会话不存在")

    try:
        memory = store.load(session_id)
    except SessionStoreError as exc:
        raise ApiError(400, "session_invalid", "会话不存在或已损坏") from exc

    scanner = DlpScanner()
    messages: list[MessageView] = []
    for raw in memory.raw_messages():
        if raw.role == "tool":
            continue  # 工具原始结果不进历史接口
        content = redact_text(raw.content)
        if scanner.scan(content).blocked:
            content = _BLOCKED_CONTENT
        messages.append(MessageView(role=raw.role, content=content))
    return SessionMessagesResponse(session_id=session_id, messages=messages)


@router.get("/v1/traces", response_model=TraceListResponse)
async def list_traces(
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
    session_id: str = "",
    limit: int = 50,
) -> TraceListResponse:
    """当前用户的 Trace 列表；可按 session_id 过滤。"""
    deps = get_deps(request)
    trace_store = deps.resources_for(user.tenant_id).trace_store
    capped = max(1, min(limit, _MAX_TRACE_LIMIT))
    infos = [
        info
        for info in trace_store.list_traces()
        if info.user_id == user.user_id and (not session_id or info.session_id == session_id)
    ][:capped]
    return TraceListResponse(
        traces=[
            TraceSummary(
                trace_id=info.trace_id,
                session_id=info.session_id,
                user_input=_truncate(redact_text(info.user_input), _MAX_TRACE_INPUT_CHARS),
                used_fallback=info.used_fallback,
                total_latency_ms=info.total_latency_ms,
                saved_at=info.saved_at,
            )
            for info in infos
        ]
    )


@router.get("/v1/traces/{trace_id}", response_model=TraceView)
async def get_trace(
    trace_id: str,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> TraceView:
    """单条 Trace 的脱敏报告；非本人一律 404。"""
    deps = get_deps(request)
    try:
        trace = deps.resources_for(user.tenant_id).trace_store.load(trace_id)
    except TraceStoreError as exc:
        raise ApiError(404, "trace_not_found", "Trace 不存在") from exc
    if trace.user_id != user.user_id:
        raise ApiError(404, "trace_not_found", "Trace 不存在")

    report = redact_text(render_trace_report(trace))
    if DlpScanner().scan(report).blocked:
        report = _BLOCKED_REPORT
    return TraceView(
        trace_id=str(trace.trace_id),
        session_id=trace.session_id,
        user_id=trace.user_id,
        user_input=redact_text(trace.user_input),
        final_response=redact_text(trace.final_response),
        report_text=report,
    )
