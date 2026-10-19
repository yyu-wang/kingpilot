"""SSE 流式对话（M9-T02）。

设计边界：
- 事件只暴露阶段/工具元数据；工具参数与结果永不进事件
- ``final`` 在 DLP 与会话落盘之后发送，保证客户端看到的文本已过出域扫描
- 流开始前的认证/参数错误走 JSON；流开始后的失败走 ``error`` 事件 + ``done``
- 客户端断连 → 取消生产者任务，不保存半截会话
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from agent.events import AgentEvent
from agent.session_store import SessionStore
from api.deps import AppDeps, get_current_user, get_deps
from api.errors import ApiError
from api.schemas import ChatRequest
from infrastructure.exceptions import (
    BudgetExceededError,
    KingPilotError,
    SessionStoreError,
)
from models.user import UserContext

# 无事件时的心跳间隔；保证代理/浏览器不会把长连接判定为空闲
_HEARTBEAT_SECONDS = 15.0

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx 默认缓冲会破坏「逐事件可见」，显式关闭
    "X-Accel-Buffering": "no",
}

router = APIRouter()


@router.post("/v1/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    user: Annotated[UserContext, Depends(get_current_user)],
) -> StreamingResponse:
    """流式对话：认证与路径校验先完成，再进入事件流。"""
    deps = get_deps(request)
    message = payload.message.strip()
    if not message:
        raise ApiError(422, "empty_message", "消息不能为空")

    session_id = payload.session_id or uuid4().hex[:12]
    store = deps.store_for(user.user_id, tenant_id=user.tenant_id)
    try:
        store.path_for(session_id)  # 流开始前挡住路径穿越
    except SessionStoreError as exc:
        raise ApiError(400, "session_invalid", "会话 ID 非法") from exc

    return StreamingResponse(
        _stream_chat(deps, user, store, session_id, message),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def _stream_chat(
    deps: AppDeps,
    user: UserContext,
    store: SessionStore,
    session_id: str,
    message: str,
) -> AsyncGenerator[str, None]:
    """生产者跑主循环，消费者按事件推 SSE；断连时取消生产者。"""
    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

    def sink(event: AgentEvent) -> None:
        """同步旁路：让 Core 不感知队列与 SSE。"""
        queue.put_nowait(event)

    async def produce() -> None:
        try:
            # 与同步接口一致：load → run → save 整段串行（M9-T01 D5）
            async with deps.locks.get(f"{user.tenant_id}:{user.user_id}:{session_id}"):
                try:
                    memory = store.load(session_id)
                except SessionStoreError:
                    queue.put_nowait(
                        AgentEvent(
                            event="error",
                            code="session_invalid",
                            message="会话不存在或已损坏",
                        )
                    )
                    return

                core = deps.build_core(memory, user=user, event_sink=sink)
                response = await core.run(message, user_context=user)

                try:
                    store.save(memory)
                except SessionStoreError:
                    queue.put_nowait(
                        AgentEvent(
                            event="error",
                            code="session_write_failed",
                            message="会话保存失败，请重试",
                        )
                    )
                    return

                # final 必须在 DLP 后、落盘后发送
                queue.put_nowait(
                    AgentEvent(
                        event="final",
                        content=response.content,
                        trace_id=str(response.trace_id),
                        session_id=session_id,
                        blocked=response.blocked,
                        cancelled=response.cancelled,
                        hallucination_warning=response.hallucination_warning,
                        approval_pending=response.approval_pending,
                        approval_id=response.approval_id,
                    )
                )

                sampler = deps.resources_for(user.tenant_id).sampler
                if sampler is not None:
                    try:
                        sampler.maybe_sample(response, session_id=session_id)
                    except Exception as exc:
                        logger.warning("在线抽样失败（不影响流式响应）: {}", exc)
        except Exception as exc:
            queue.put_nowait(_error_event(exc))
        finally:
            queue.put_nowait(AgentEvent(event="done", session_id=session_id))
            queue.put_nowait(None)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if event is None:
                break
            yield _format_sse(event)
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _format_sse(event: AgentEvent) -> str:
    """事件 → SSE 帧；空字段由 exclude_defaults 自动省略。"""
    payload = event.model_dump(exclude_defaults=True)
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event.event}\ndata: {data}\n\n"


def _error_event(exc: Exception) -> AgentEvent:
    """流内错误映射：只返回安全文案，不带异常 context/堆栈。"""
    if isinstance(exc, ApiError):
        return AgentEvent(event="error", code=exc.code, message=exc.message)
    if isinstance(exc, BudgetExceededError):
        return AgentEvent(
            event="error",
            code="budget_exhausted",
            message="服务预算已耗尽，请稍后重试",
        )
    if isinstance(exc, KingPilotError):
        logger.warning("流式请求业务异常: {}", exc)
        return AgentEvent(event="error", code="agent_error", message="Agent 处理失败")
    logger.exception("流式请求未预期异常")
    return AgentEvent(event="error", code="internal_error", message="服务内部错误")
