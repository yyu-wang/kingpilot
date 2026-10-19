"""FastAPI app factory 与 lifespan（M9-T01）。

资源生命周期：
- 进程级资源在 lifespan 内创建：Provider / KingdeeClient / SafetyLayer / Trace / Audit
- 测试可注入 Provider/Client：``create_app(settings, provider=..., client=...)``
- ``AsyncExitStack`` 统一管理「谁创建、谁关闭」，注入时不动外部资源
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from agent.llm import LLMProvider
from agent.providers import get_provider
from agent.tools import ToolRegistry, build_default_registry
from api.approvals import router as approvals_router
from api.auth import ApiKeyDirectory
from api.console import mount_console, security_headers
from api.dashboard import router as dashboard_router
from api.deps import AppDeps, DenyConfirmationIO
from api.errors import install_error_handlers
from api.routes import router
from api.streaming import router as streaming_router
from api.traces import router as traces_router
from config.settings import Settings, get_settings
from governance.approval import ApprovalService, FileApprovalStore
from governance.approval_executor import (
    ApprovalExecution,
    ApprovalExecutor,
    NoopSnapshotProvider,
    RegistryToolExecutor,
)
from governance.approval_gateway import ApprovalGatewayImpl
from governance.cost_engine import CostEngine, CostRecord
from governance.dashboard import DashboardService
from governance.online_sampler import OnlineSampler
from governance.prompt_registry import PromptRegistry
from infrastructure.logger import setup_logging
from kingdee.client import KingdeeClient
from kingdee.idempotency import IdempotencyService
from kingdee.write_coordinator import WriteCoordinator
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.audit import AuditEvent
from observability.alerts import AlertEngine
from observability.audit import AuditLogger, JsonlAuditReader, JsonlAuditSink
from observability.tracing import TraceStore
from safety.engine import SafetyLayer


def _build_cost_sink(audit: AuditLogger) -> Callable[[CostRecord], None]:
    """成本明细写统一审计流，供看板按天聚合（M9-T06）。"""

    def sink(record: CostRecord) -> None:
        audit.emit(
            AuditEvent(
                event_type="cost.recorded",
                action="llm_call",
                status="success",
                detail={
                    "route": record.route_name,
                    "tier": record.tier,
                    "model_id": record.model_id,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "cost_usd": record.cost_usd,
                },
            )
        )

    return sink


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """构建进程级依赖；退出时按进入顺序反向释放。"""
    settings: Settings = app.state.settings
    setup_logging()

    # 审计先于 Provider：预算降档/耗尽事件也要进统一审计流（M7-T04）
    audit = AuditLogger(JsonlAuditSink(settings.audit_log_path))
    audit.subscribe(AlertEngine().observe)
    cost_engine = CostEngine(sink=_build_cost_sink(audit))

    async with AsyncExitStack() as stack:
        provider: LLMProvider | None = app.state.provider_override
        if provider is None:
            prompts = PromptRegistry(settings.prompts_dir)
            provider = get_provider(
                settings,
                audit=audit,
                prompts=prompts,
                rollout_key="",
                cost_engine=cost_engine,
            )
            close_provider = getattr(provider, "aclose", None)
            if callable(close_provider):
                stack.push_async_callback(close_provider)

        client: KingdeeClient | None = app.state.client_override
        if client is None:
            client = await stack.enter_async_context(KingdeeClient(settings))

        registry = app.state.registry_override or build_default_registry()
        approvals = ApprovalService(
            FileApprovalStore(settings.approval_store_dir),
            audit=audit,
            ttl_seconds=settings.approval_ttl_seconds,
        )
        gateway = ApprovalGatewayImpl(approvals, registry)
        safety = SafetyLayer(
            registry,
            client,
            DenyConfirmationIO(),
            audit=audit,
            approval_gateway=gateway,
        )
        trace_store = TraceStore(settings.trace_store_dir)
        ledger = WriteLedger(SqliteWriteLedgerStore(settings.ledger_db_path), audit=audit)
        writer = WriteCoordinator(ledger, idempotency=IdempotencyService())
        execution: ApprovalExecution = app.state.approval_execution_override or ApprovalExecutor(
            approvals,
            trace_store=trace_store,
            writer=writer,
            executor=RegistryToolExecutor(registry, client),
            snapshots=NoopSnapshotProvider(),
            ledger=ledger,
        )
        dashboard = DashboardService(
            trace_store,
            JsonlAuditReader(settings.audit_log_path),
            ledger,
            timezone=settings.dashboard_timezone,
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
            trace_store=trace_store,
            audit=audit,
            sampler=sampler,
            api_keys=ApiKeyDirectory.from_settings(settings),
            session_root=settings.session_store_dir,
            approvals=approvals,
            approval_execution=execution,
            dashboard=dashboard,
        )
        yield


def create_app(
    settings: Settings | None = None,
    *,
    provider: LLMProvider | None = None,
    client: KingdeeClient | None = None,
    registry: ToolRegistry | None = None,
    approval_execution: ApprovalExecution | None = None,
) -> FastAPI:
    """App factory：生产零参数启动，测试注入 Settings/Provider/Client/Registry/执行器。"""
    resolved = settings or get_settings()
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
    mount_console(app)
    return app
