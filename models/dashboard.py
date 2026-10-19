"""运营看板聚合模型（M9-T06）。

只承载聚合数字：不含 user_id / trace_id / 用户输入 / 工具参数。
"""

from datetime import date

from pydantic import Field

from models.base import KingPilotBaseModel


class BlockedStats(KingPilotBaseModel):
    """拦截事件分类计数。"""

    guard: int = 0
    permission: int = 0
    dlp: int = 0
    total: int = 0


class RiskDistribution(KingPilotBaseModel):
    """风险档位分布。"""

    auto: int = 0
    confirm: int = 0
    dual_approve: int = 0
    read_only: int = 0
    unknown: int = 0


class DashboardMetrics(KingPilotBaseModel):
    """一组指标（每日或总计）。"""

    requests: int = 0
    hallucination_count: int = 0
    hallucination_rate: float = 0.0
    fallback_count: int = 0
    fallback_rate: float = 0.0
    blocked: BlockedStats = Field(default_factory=BlockedStats)
    risk_distribution: RiskDistribution = Field(default_factory=RiskDistribution)
    cost_usd: float = 0.0
    tokens: int = 0
    llm_calls: int = 0
    write_success: int = 0
    write_failed: int = 0
    avg_latency_ms: float = 0.0


class DailyDashboard(DashboardMetrics):
    """单日指标。"""

    date: date


class DashboardOverview(KingPilotBaseModel):
    """看板总览：区间 + 总计 + 每日序列。"""

    from_date: date
    to_date: date
    timezone: str
    totals: DashboardMetrics
    daily: list[DailyDashboard] = Field(default_factory=list)
