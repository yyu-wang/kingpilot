"""FastAPI app factory 与 lifespan（M9-T01，M9-T11 多租户化）。

资源生命周期：
- 进程级共享：Audit（租户感知 sink）/ Client / Registry / Prompt / SecretProvider / API Key 目录
- 每租户资源：由 ``AppDeps.resources_for`` 懒加载（Provider/Trace/Ledger/审批/看板/采样）
- 测试可注入 Provider/Client/Registry/审批执行器
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from agent.llm import LLMProvider
from agent.tools import ToolRegistry, build_default_registry
from api.admin import router as admin_router
from api.approvals import router as approvals_router
from api.auth import ApiKeyDirectory
from api.badcases import router as badcases_router
from api.console import mount_console, security_headers
from api.dashboard import router as dashboard_router
from api.deps import AppDeps
from api.errors import install_error_handlers
from api.routes import router
from api.streaming import router as streaming_router
from api.traces import router as traces_router
from config.settings import Settings, get_settings
from erp.factory import build_erp_client
from erp.protocol import ErpClient
from governance.approval_executor import ApprovalExecution
from governance.prompt_registry import PromptRegistry
from infrastructure.logger import setup_logging
from infrastructure.secrets import build_secret_provider
from observability.alerts import AlertEngine
from observability.audit import AuditLogger, JsonlAuditSink


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """构建进程级依赖；租户资源按需创建，退出时统一释放 Provider。"""
    settings: Settings = app.state.settings
    setup_logging()

    # 审计先于 Provider：预算降档/耗尽事件也要进统一审计流（M7-T04）
    audit = AuditLogger(JsonlAuditSink(settings.audit_log_path, tenant_aware=True))
    audit.subscribe(AlertEngine().observe)
    secrets = build_secret_provider(settings)
    prompts = PromptRegistry(settings.prompts_dir)

    async with AsyncExitStack() as stack:
        client: ErpClient | None = app.state.client_override
        if client is None:
            client = await stack.enter_async_context(build_erp_client(settings, secrets=secrets))

        registry = app.state.registry_override or build_default_registry()
        deps = AppDeps(
            settings=settings,
            client=client,
            registry=registry,
            audit=audit,
            api_keys=ApiKeyDirectory.from_settings(settings),
            prompts=prompts,
            secrets=secrets,
            provider_override=app.state.provider_override,
            approval_execution_override=app.state.approval_execution_override,
        )
        app.state.deps = deps
        try:
            yield
        finally:
            await deps.aclose()


def create_app(
    settings: Settings | None = None,
    *,
    provider: LLMProvider | None = None,
    client: ErpClient | None = None,
    registry: ToolRegistry | None = None,
    approval_execution: ApprovalExecution | None = None,
) -> FastAPI:
    """App factory：生产零参数启动，测试注入 Settings/Provider/Client/Registry/执行器。"""
    resolved = (settings or get_settings()).with_local_sandbox_defaults()
    app = FastAPI(title="KingPilot API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.provider_override = provider
    app.state.client_override = client
    app.state.registry_override = registry
    app.state.approval_execution_override = approval_execution
    app.middleware("http")(security_headers)
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(streaming_router)
    app.include_router(traces_router)
    app.include_router(approvals_router)
    app.include_router(dashboard_router)
    app.include_router(badcases_router)
    app.include_router(admin_router)
    mount_console(app)
    return app
