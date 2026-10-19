"""成本路由与用量记账（M7-T03，方案 §5.4 支柱十二）。

职责分三层：
- ``estimate_cost``：token × 单价 → 单次调用成本
- ``CostEngine``：累计成本记录，按档位聚合（T04 预算熔断的数据源）
- ``RoutingProvider``：按当前档位选择底层 Provider，并在用量回调里记账

档位切换只动配置：``ROUTING_CONFIG``（档位 → 路由名）与
``Settings.llm_tier``；``set_tier()`` 供 T04 预算降档调用。
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from loguru import logger

from agent.llm import TokenUsageSink
from agent.providers import OpenAICompatibleProvider
from config.model_registry import (
    MODEL_REGISTRY,
    ROUTING_CONFIG,
    TIERS,
    ModelRoute,
    ModelTier,
)
from config.settings import Settings
from governance.budget import BudgetDecision, BudgetPolicy
from governance.prompt_registry import PromptRegistry
from infrastructure.exceptions import BudgetExceededError, LLMError
from models.audit import AuditEvent
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    PlanResult,
    ReflectionResult,
    SummaryDraft,
    TokenUsage,
    ToolSpec,
)
from observability.audit import AuditLogger

_TOKENS_PER_MILLION = 1_000_000
_COST_PRECISION = 6


@dataclass(frozen=True, slots=True)
class CostRecord:
    """单次 LLM 调用的成本记录。"""

    route_name: str
    tier: ModelTier
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    recorded_at: datetime


CostRecordSink = Callable[[CostRecord], None]


def estimate_cost(route: ModelRoute, usage: TokenUsage) -> float:
    """按每百万 token 单价估算单次调用成本（USD）。"""
    raw = (
        usage.prompt_tokens * route.input_cost_per_1m_usd
        + usage.completion_tokens * route.output_cost_per_1m_usd
    ) / _TOKENS_PER_MILLION
    return round(raw, _COST_PRECISION)


class CostEngine:
    """进程内成本账本：逐次记录 + 按档位聚合。

    只保存进程内记录，落盘与预算熔断归 M7-T04；``sink`` 可接告警或
    持久化通道，异常不影响 LLM 调用主链路。
    """

    def __init__(self, *, sink: CostRecordSink | None = None) -> None:
        self._sink = sink
        self._records: list[CostRecord] = []

    @property
    def records(self) -> tuple[CostRecord, ...]:
        """已记录的全部成本明细（只读快照）。"""
        return tuple(self._records)

    @property
    def total_cost_usd(self) -> float:
        """累计成本（USD）。"""
        return round(sum(record.cost_usd for record in self._records), _COST_PRECISION)

    @property
    def total_tokens(self) -> int:
        """累计 token 数。"""
        return sum(record.prompt_tokens + record.completion_tokens for record in self._records)

    def record(self, route: ModelRoute, usage: TokenUsage) -> CostRecord:
        """记录一次调用并返回明细。"""
        record = CostRecord(
            route_name=route.name,
            tier=route.tier,
            model_id=route.model_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=estimate_cost(route, usage),
            recorded_at=datetime.now(UTC),
        )
        self._records.append(record)
        logger.debug(
            "LLM 成本 | route={} tier={} tokens={}/{} cost_usd={}",
            record.route_name,
            record.tier,
            record.prompt_tokens,
            record.completion_tokens,
            record.cost_usd,
        )
        if self._sink is not None:
            try:
                self._sink(record)
            except Exception as exc:  # 记账旁路失败不能打断业务
                logger.warning("成本记录 sink 异常: {}", exc)
        return record

    def summary(self) -> dict[str, dict[str, float | int]]:
        """按档位聚合：calls / prompt_tokens / completion_tokens / cost_usd。"""
        summary: dict[str, dict[str, float | int]] = {
            tier: {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
            }
            for tier in TIERS
        }
        for record in self._records:
            bucket = summary[record.tier]
            bucket["calls"] = cast(int, bucket["calls"]) + 1
            bucket["prompt_tokens"] = cast(int, bucket["prompt_tokens"]) + record.prompt_tokens
            bucket["completion_tokens"] = (
                cast(int, bucket["completion_tokens"]) + record.completion_tokens
            )
            bucket["cost_usd"] = round(
                cast(float, bucket["cost_usd"]) + record.cost_usd, _COST_PRECISION
            )
        return summary


class RoutingProvider:
    """按档位路由的 LLMProvider 实现。

    - 档位 → 路由名 → 真实端点，两级查表都在构造期校验（fail-fast）
    - 底层 Provider 延迟创建：未使用的档位不建连接、不要求凭证
    - 每次真实调用经 usage_sink 自动记一条 ``CostRecord``

    ``set_tier()`` 是 T04 预算降档的唯一入口：只切指针，不重建连接。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        tier: ModelTier | None = None,
        cost_engine: CostEngine | None = None,
        usage_sink: TokenUsageSink | None = None,
        budget: BudgetPolicy | None = None,
        audit: AuditLogger | None = None,
        prompts: PromptRegistry | None = None,
        rollout_key: str = "",
        routes: Mapping[str, ModelRoute] = MODEL_REGISTRY,
        routing: Mapping[ModelTier, str] | None = None,
    ) -> None:
        self._settings = settings
        self._tier = _validate_tier(tier or settings.llm_tier)
        self._routes = dict(routes)
        self._routing = dict(routing if routing is not None else ROUTING_CONFIG)
        self._validate_routing()
        self._cost = cost_engine or CostEngine()
        self._usage_sink = usage_sink
        # 预算默认按 Settings 构造；注入自定义策略供单测/会话级预算使用
        self._budget = budget or BudgetPolicy(
            settings.llm_budget_usd,
            downgrade_ratio=settings.llm_budget_downgrade_ratio,
        )
        self._audit = audit
        # Prompt 版本/灰度由底层 Provider 消费；None = 内置常量（M8-T04）
        self._prompts = prompts
        self._rollout_key = rollout_key
        self._providers: dict[ModelTier, OpenAICompatibleProvider] = {}

    @property
    def current_tier(self) -> ModelTier:
        """当前档位。"""
        return self._tier

    @property
    def cost_engine(self) -> CostEngine:
        """成本账本（预算判断与测试断言使用）。"""
        return self._cost

    def set_tier(self, tier: ModelTier) -> None:
        """切换档位（T04 预算降档入口）。"""
        self._tier = _validate_tier(tier)

    def _validate_routing(self) -> None:
        """校验档位映射完整且指向已注册路由。"""
        for tier in TIERS:
            route_name = self._routing.get(tier)
            if route_name is None:
                raise LLMError(
                    f"ROUTING_CONFIG 缺少档位: {tier}",
                    context={"known_tiers": ", ".join(TIERS)},
                )
            if route_name not in self._routes:
                known = ", ".join(sorted(self._routes))
                raise LLMError(
                    f"档位 {tier} 指向未知路由: {route_name}",
                    context={"known_routes": known},
                )

    def _provider_for(self, tier: ModelTier) -> OpenAICompatibleProvider:
        """取（或惰性创建）该档位的底层 Provider。"""
        provider = self._providers.get(tier)
        if provider is not None:
            return provider
        route = self._routes[self._routing[tier]]
        provider = OpenAICompatibleProvider(
            self._settings,
            route=route,
            usage_sink=self._make_usage_sink(route),
            prompts=self._prompts,
            rollout_key=self._rollout_key,
        )
        self._providers[tier] = provider
        return provider

    def _make_usage_sink(self, route: ModelRoute) -> TokenUsageSink:
        """把底层用量回调接到成本账本，再转发外部 sink。"""

        def sink(usage: TokenUsage) -> None:
            self._cost.record(route, usage)
            if self._usage_sink is not None:
                self._usage_sink(usage)

        return sink

    # ── 预算熔断（M7-T04）──

    def _prepare_call(self) -> None:
        """调用前检查预算：必要时降档；经济档耗尽则拒绝调用。"""
        decision = self._budget.evaluate(self._cost.total_cost_usd, self._tier)
        if decision.action == "keep":
            return
        if decision.action == "downgrade":
            previous = self._tier
            self.set_tier(decision.tier)
            self._emit_budget_event("budget.downgraded", previous, decision, status="success")
            logger.warning(
                "模型预算降档 | {} → {} spent_usd={} budget_usd={}",
                previous,
                decision.tier,
                decision.spent_usd,
                self._budget.budget_usd,
            )
            return

        self._emit_budget_event("budget.exhausted", self._tier, decision, status="blocked")
        raise BudgetExceededError(
            "LLM 预算已耗尽，请稍后重试或联系管理员",
            context={
                "spent_usd": decision.spent_usd,
                "budget_usd": self._budget.budget_usd,
            },
        )

    def _emit_budget_event(
        self,
        event_type: Literal["budget.downgraded", "budget.exhausted"],
        previous_tier: ModelTier,
        decision: BudgetDecision,
        *,
        status: Literal["success", "blocked"],
    ) -> None:
        """写预算审计事件；未注入审计时只保留告警日志。"""
        if self._audit is None:
            return
        self._audit.emit(
            AuditEvent(
                event_type=event_type,
                status=status,
                detail={
                    "previous_tier": previous_tier,
                    "new_tier": decision.tier,
                    "spent_usd": decision.spent_usd,
                    "budget_usd": self._budget.budget_usd,
                },
            )
        )

    # ── LLMProvider 协议实现：全部委托当前档位 Provider ──

    async def plan(self, context: LLMContext, tools: Sequence[ToolSpec]) -> PlanResult:
        self._prepare_call()
        return await self._provider_for(self._tier).plan(context, tools)

    async def reflect(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> ReflectionResult:
        self._prepare_call()
        return await self._provider_for(self._tier).reflect(context, execution, memory)

    async def generate_response(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> str:
        self._prepare_call()
        return await self._provider_for(self._tier).generate_response(context, execution, memory)

    async def summarize(self, messages: Sequence[LLMMessage]) -> SummaryDraft:
        self._prepare_call()
        return await self._provider_for(self._tier).summarize(messages)

    async def aclose(self) -> None:
        """关闭所有已创建的底层连接池。"""
        for provider in self._providers.values():
            await provider.aclose()
        self._providers.clear()

    async def __aenter__(self) -> "RoutingProvider":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def _validate_tier(tier: str) -> ModelTier:
    """档位闭集校验：配置错误在构造/切换时暴露。"""
    if tier not in TIERS:
        raise LLMError(
            f"未知模型档位: {tier}",
            context={"known_tiers": ", ".join(TIERS)},
        )
    return tier
