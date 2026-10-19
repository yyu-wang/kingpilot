"""FastAPI 依赖容器（M9-T01，M9-T11 多租户化）。

职责：
- 进程级共享资源：Client / Registry / Audit / API Key 目录 / Prompt / SecretProvider
- 每租户资源：Provider（含预算与 Prompt 灰度）、Trace、Ledger、审批、Badcase、看板、采样
- 每个请求只构造 ``AgentCore`` 与 ``MemoryStore``，不复制业务逻辑
- 同一租户同一会话用进程内锁串行（读-改-写不能并发）

默认租户（default）保留既有属性（``deps.trace_store`` 等），
保证单租户脚本与测试平滑升级。
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Annotated
from weakref import WeakValueDictionary

from fastapi import Header, Request

from agent.core import AgentCore
from agent.events import AgentEventSink
from agent.llm import LLMProvider
from agent.memory import MemoryStore
from agent.providers import get_provider
from agent.session_store import SessionStore
from agent.tools import ToolRegistry
from api.auth import ApiKeyDirectory
from api.errors import ApiError
from config.settings import Settings
from config.tenants import tenant_profile
from governance.approval import ApprovalService, FileApprovalStore
from governance.approval_executor import (
    ApprovalExecution,
    ApprovalExecutor,
    NoopSnapshotProvider,
    RegistryToolExecutor,
)
from governance.approval_gateway import ApprovalGatewayImpl
from governance.badcase import BadcaseService, FileBadcaseStore
from governance.budget import BudgetPolicy
from governance.cost_engine import CostEngine, CostRecord
from governance.dashboard import DashboardService
from governance.online_sampler import OnlineSampler
from governance.prompt_registry import PromptRegistry
from infrastructure.secrets import SecretProvider
from infrastructure.tenancy import tenant_paths, tenant_scope
from kingdee.client import KingdeeClient
from kingdee.idempotency import IdempotencyService
from kingdee.write_coordinator import WriteCoordinator
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.audit import AuditEvent
from models.tenant import DEFAULT_TENANT, TenantContext
from models.user import UserContext
from observability.audit import AuditLogger, JsonlAuditReader
from observability.tracing import TraceStore
from safety.engine import SafetyLayer


class DenyConfirmationIO:
    """API 模式的确认通道：一律拒绝。

    审批中心（M9-T04/T05）落地后，确认类写操作改走审批单。
    """

    async def ask(self, prompt: str) -> bool:
        return False


class SessionLocks:
    """进程内按会话串行；WeakValueDictionary 避免锁对象无限堆积。

    边界：只保护单进程；多 worker 部署需要跨进程锁。
    """

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


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


@dataclass(frozen=True)
class TenantResources:
    """一个租户独占的运行期资源。"""

    tenant: TenantContext
    provider: LLMProvider
    rollout_key: str
    cost_engine: CostEngine
    safety: SafetyLayer
    trace_store: TraceStore
    ledger: WriteLedger
    writer: WriteCoordinator
    approvals: ApprovalService
    approval_execution: ApprovalExecution
    dashboard: DashboardService
    badcases: BadcaseService
    sampler: OnlineSampler | None


@dataclass
class AppDeps:
    """进程级依赖集合（lifespan 构建，路由只读）。"""

    settings: Settings
    client: KingdeeClient
    registry: ToolRegistry
    audit: AuditLogger
    api_keys: ApiKeyDirectory
    prompts: PromptRegistry
    secrets: SecretProvider
    provider_override: LLMProvider | None = None
    approval_execution_override: ApprovalExecution | None = None
    locks: SessionLocks = field(default_factory=SessionLocks)
    _resources: dict[str, TenantResources] = field(default_factory=dict)
    _owned_providers: list[LLMProvider] = field(default_factory=list)

    # ────────────────────────── 租户资源 ──────────────────────────

    def resources_for(self, tenant_id: str) -> TenantResources:
        """取（或懒加载）租户资源；同一租户共享 Provider/账本/看板实例。"""
        tenant = TenantContext(tenant_id=tenant_id)
        cached = self._resources.get(tenant.tenant_id)
        if cached is not None:
            return cached

        paths = tenant_paths(self.settings, tenant.tenant_id)
        profile = tenant_profile(tenant.tenant_id)
        rollout_key = profile.prompt_rollout_key or tenant.tenant_id
        cost_engine = CostEngine(sink=_build_cost_sink(self.audit))

        provider = self.provider_override
        if provider is None:
            budget = BudgetPolicy(
                profile.llm_budget_usd or self.settings.llm_budget_usd,
                downgrade_ratio=self.settings.llm_budget_downgrade_ratio,
            )
            provider = get_provider(
                self.settings,
                audit=self.audit,
                prompts=self.prompts,
                rollout_key=rollout_key,
                cost_engine=cost_engine,
                secrets=self.secrets,
                budget=budget,
            )
            self._owned_providers.append(provider)

        trace_store = TraceStore(paths.trace_dir)
        ledger = WriteLedger(SqliteWriteLedgerStore(paths.ledger_path), audit=self.audit)
        writer = WriteCoordinator(ledger, idempotency=IdempotencyService())
        approvals = ApprovalService(
            FileApprovalStore(paths.approval_dir),
            audit=self.audit,
            ttl_seconds=self.settings.approval_ttl_seconds,
        )
        gateway = ApprovalGatewayImpl(approvals, self.registry)
        safety = SafetyLayer(
            self.registry,
            self.client,
            DenyConfirmationIO(),
            audit=self.audit,
            approval_gateway=gateway,
        )
        execution: ApprovalExecution = self.approval_execution_override or ApprovalExecutor(
            approvals,
            trace_store=trace_store,
            writer=writer,
            executor=RegistryToolExecutor(self.registry, self.client),
            snapshots=NoopSnapshotProvider(),
            ledger=ledger,
        )
        dashboard = DashboardService(
            trace_store,
            JsonlAuditReader(paths.audit_path),
            ledger,
            timezone=self.settings.dashboard_timezone,
        )
        badcases = BadcaseService(
            FileBadcaseStore(paths.badcase_dir),
            trace_store=trace_store,
            tools=self.registry,
            dataset_root=self.settings.eval_dataset_dir,
            audit=self.audit,
        )
        sampler = (
            OnlineSampler(
                paths.sample_dir,
                sample_rate=self.settings.online_sample_rate,
                max_per_day=self.settings.online_sample_max_per_day,
            )
            if self.settings.online_sample_rate > 0
            else None
        )

        resources = TenantResources(
            tenant=tenant,
            provider=provider,
            rollout_key=rollout_key,
            cost_engine=cost_engine,
            safety=safety,
            trace_store=trace_store,
            ledger=ledger,
            writer=writer,
            approvals=approvals,
            approval_execution=execution,
            dashboard=dashboard,
            badcases=badcases,
            sampler=sampler,
        )
        self._resources[tenant.tenant_id] = resources
        return resources

    # 兼容单租户调用方：默认指向 default 租户
    @property
    def provider(self) -> LLMProvider:
        return self.resources_for(DEFAULT_TENANT).provider

    @property
    def safety(self) -> SafetyLayer:
        return self.resources_for(DEFAULT_TENANT).safety

    @property
    def trace_store(self) -> TraceStore:
        return self.resources_for(DEFAULT_TENANT).trace_store

    @property
    def approvals(self) -> ApprovalService:
        return self.resources_for(DEFAULT_TENANT).approvals

    @property
    def approval_execution(self) -> ApprovalExecution:
        return self.resources_for(DEFAULT_TENANT).approval_execution

    @property
    def dashboard(self) -> DashboardService:
        return self.resources_for(DEFAULT_TENANT).dashboard

    @property
    def badcases(self) -> BadcaseService:
        return self.resources_for(DEFAULT_TENANT).badcases

    @property
    def sampler(self) -> OnlineSampler | None:
        return self.resources_for(DEFAULT_TENANT).sampler

    # ────────────────────────── 请求级构造 ──────────────────────────

    def store_for(self, user_id: str, *, tenant_id: str = DEFAULT_TENANT) -> SessionStore:
        """用户会话目录：先按租户，再按用户隔离。"""
        paths = tenant_paths(self.settings, tenant_id)
        return SessionStore(paths.session_root / "users" / user_id)

    def build_core(
        self,
        memory: MemoryStore,
        *,
        user: UserContext,
        event_sink: AgentEventSink | None = None,
    ) -> AgentCore:
        """每个请求构造主循环实例：记忆/鉴权是请求级，租户资源是缓存级。"""
        resources = self.resources_for(user.tenant_id)
        return AgentCore(
            resources.provider,
            self.registry,
            self.client,
            self.settings,
            memory=memory,
            safety=resources.safety,
            trace_store=resources.trace_store,
            audit=self.audit,
            event_sink=event_sink,
        )

    async def aclose(self) -> None:
        """释放本进程创建的 Provider 连接池（注入的 Provider 由调用方管理）。"""
        for provider in self._owned_providers:
            close = getattr(provider, "aclose", None)
            if callable(close):
                await close()
        self._owned_providers.clear()


def get_deps(request: Request) -> AppDeps:
    """取 lifespan 构建好的依赖；未就绪返回 503。"""
    deps = getattr(request.app.state, "deps", None)
    if not isinstance(deps, AppDeps):
        raise ApiError(503, "not_ready", "服务尚未就绪")
    return deps


async def get_current_user(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> AsyncIterator[UserContext]:
    """API Key → ``UserContext``；并在整个请求生命周期内设置租户上下文。"""
    if not x_api_key:
        raise _unauthorized()
    identity = get_deps(request).api_keys.authenticate(x_api_key)
    if identity is None:
        raise _unauthorized()
    user = UserContext(
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        role=identity.role,
        data_scope=identity.data_scope,
        ip_address=request.client.host if request.client else "",
    )
    # yield 依赖：租户上下文覆盖路由内的审计/存储/预算读取
    with tenant_scope(user.tenant_id):
        yield user


def _unauthorized() -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        "无效或缺失的 API Key",
        headers={"WWW-Authenticate": "X-API-Key"},
    )
