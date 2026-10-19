"""FastAPI app factory 与 lifespan（M9-T01）。

资源生命周期：
- 进程级资源在 lifespan 内创建：Provider / KingdeeClient / SafetyLayer / Trace / Audit
- 测试可注入 Provider/Client：``create_app(settings, provider=..., client=...)``
- ``AsyncExitStack`` 统一管理「谁创建、谁关闭」，注入时不动外部资源
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from agent.llm import LLMProvider
from agent.providers import get_provider
from agent.tools import build_default_registry
from api.approvals import router as approvals_router
from api.auth import ApiKeyDirectory
from api.console import mount_console, security_headers
from api.deps import AppDeps, DenyConfirmationIO
from api.errors import install_error_handlers
from api.routes import router
from api.streaming import router as streaming_router
from api.traces import router as traces_router
from config.settings import Settings, get_settings
from governance.approval import ApprovalService, FileApprovalStore
from governance.online_sampler import OnlineSampler
from governance.prompt_registry import PromptRegistry
from infrastructure.logger import setup_logging
from kingdee.client import KingdeeClient
from observability.alerts import AlertEngine
from observability.audit import AuditLogger, JsonlAuditSink
from observability.tracing import TraceStore
from safety.engine import SafetyLayer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """构建进程级依赖；退出时按进入顺序反向释放。"""
    settings: Settings = app.state.settings
    setup_logging()

    # 审计先于 Provider：预算降档/耗尽事件也要进统一审计流（M7-T04）
    audit = AuditLogger(JsonlAuditSink(settings.audit_log_path))
    audit.subscribe(AlertEngine().observe)

    async with AsyncExitStack() as stack:
        provider: LLMProvider | None = app.state.provider_override
        if provider is None:
            prompts = PromptRegistry(settings.prompts_dir)
            provider = get_provider(settings, audit=audit, prompts=prompts, rollout_key="")
            close_provider = getattr(provider, "aclose", None)
            if callable(close_provider):
                stack.push_async_callback(close_provider)

        client: KingdeeClient | None = app.state.client_override
        if client is None:
            client = await stack.enter_async_context(KingdeeClient(settings))

        registry = build_default_registry()
        safety = SafetyLayer(registry, client, DenyConfirmationIO(), audit=audit)
        approvals = ApprovalService(
            FileApprovalStore(settings.approval_store_dir),
            audit=audit,
            ttl_seconds=settings.approval_ttl_seconds,
        )
        sampler = (
            OnlineSampler(
                settings.online_sample_dir,
                sample_rate=settings.online_sample_rate,
                max_per_day=settings.online_sample_max_per_day,
            )
            if settings.online_sample_rate > 0
            else None
        )

        app.state.deps = AppDeps(
            settings=settings,
            provider=provider,
            client=client,
            registry=registry,
            safety=safety,
            trace_store=TraceStore(settings.trace_store_dir),
            audit=audit,
            sampler=sampler,
            api_keys=ApiKeyDirectory.from_settings(settings),
            session_root=settings.session_store_dir,
            approvals=approvals,
        )
        yield


def create_app(
    settings: Settings | None = None,
    *,
    provider: LLMProvider | None = None,
    client: KingdeeClient | None = None,
) -> FastAPI:
    """App factory：生产零参数启动，测试注入 Settings/Provider/Client。"""
    resolved = settings or get_settings()
    app = FastAPI(title="KingPilot API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.provider_override = provider
    app.state.client_override = client
    app.middleware("http")(security_headers)
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(streaming_router)
    app.include_router(traces_router)
    app.include_router(approvals_router)
    mount_console(app)
    return app
