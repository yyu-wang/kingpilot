"""FastAPI 依赖容器（M9-T01）。

职责：
- 进程级资源（Provider / KingdeeClient / SafetyLayer / Trace / Audit）挂在 ``AppDeps``
- 每个请求只构造 ``AgentCore`` 与 ``MemoryStore``，不复制业务逻辑
- 同一用户同一会话用进程内锁串行（读-改-写不能并发）
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from weakref import WeakValueDictionary

from fastapi import Header, Request

from agent.core import AgentCore
from agent.events import AgentEventSink
from agent.llm import LLMProvider
from agent.memory import MemoryStore
from agent.session_store import SessionStore
from agent.tools import ToolRegistry
from api.auth import ApiKeyDirectory
from api.errors import ApiError
from config.settings import Settings
from governance.approval import ApprovalService
from governance.online_sampler import OnlineSampler
from kingdee.client import KingdeeClient
from models.user import UserContext
from observability.audit import AuditLogger
from observability.tracing import TraceStore
from safety.engine import SafetyLayer


class DenyConfirmationIO:
    """API 模式的确认通道：一律拒绝。

    审批中心（M9-T04/T05）落地前，API 只读；确认类写操作不执行。
    """

    async def ask(self, prompt: str) -> bool:
        return False


class SessionLocks:
    """进程内按会话串行；WeakValueDictionary 避免锁对象无限堆积。

    边界：只保护单进程；多 worker 部署需要跨进程锁（归 M9-T11）。
    """

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


@dataclass
class AppDeps:
    """进程级依赖集合（lifespan 构建，路由只读）。"""

    settings: Settings
    provider: LLMProvider
    client: KingdeeClient
    registry: ToolRegistry
    safety: SafetyLayer
    trace_store: TraceStore
    audit: AuditLogger
    sampler: OnlineSampler | None
    api_keys: ApiKeyDirectory
    session_root: Path
    approvals: ApprovalService
    locks: SessionLocks = field(default_factory=SessionLocks)

    def store_for(self, user_id: str) -> SessionStore:
        """每个用户独立的会话根目录（路径级隔离）。"""
        return SessionStore(self.session_root / "users" / user_id)

    def build_core(
        self,
        memory: MemoryStore,
        *,
        event_sink: AgentEventSink | None = None,
    ) -> AgentCore:
        """每个请求构造主循环实例：记忆是请求级状态，Provider 等是进程级。

        ``event_sink`` 仅流式请求注入（M9-T02）；None 时与同步接口完全一致。
        """
        return AgentCore(
            self.provider,
            self.registry,
            self.client,
            self.settings,
            memory=memory,
            safety=self.safety,
            trace_store=self.trace_store,
            audit=self.audit,
            event_sink=event_sink,
        )


def get_deps(request: Request) -> AppDeps:
    """取 lifespan 构建好的依赖；未就绪返回 503。"""
    deps = getattr(request.app.state, "deps", None)
    if not isinstance(deps, AppDeps):
        raise ApiError(503, "not_ready", "服务尚未就绪")
    return deps


async def get_current_user(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> UserContext:
    """API Key → ``UserContext``；缺失/错误统一 401，不区分原因。"""
    if not x_api_key:
        raise _unauthorized()
    identity = get_deps(request).api_keys.authenticate(x_api_key)
    if identity is None:
        raise _unauthorized()
    return UserContext(
        user_id=identity.user_id,
        role=identity.role,
        data_scope=identity.data_scope,
        ip_address=request.client.host if request.client else "",
    )


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        "无效或缺失的 API Key",
        headers={"WWW-Authenticate": "X-API-Key"},
    )
