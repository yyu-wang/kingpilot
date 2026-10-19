"""运营看板聚合（M9-T06）。

数据源分工：
- Trace：请求量 / 幻觉 / 降级 / 风险 / 延迟
- Audit：拦截事件 + cost.recorded 成本事件
- Ledger：写操作成功/失败

输出只含聚合数字；不返回 user_id / trace_id / 输入原文。
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from models.audit import AuditEvent
from models.dashboard import (
    BlockedStats,
    DailyDashboard,
    DashboardMetrics,
    DashboardOverview,
    RiskDistribution,
)
from models.ledger import WriteRecord
from observability.tracing import TraceInfo

_MAX_RANGE_DAYS = 90
_RATE_PRECISION = 4
_COST_PRECISION = 6
_LATENCY_PRECISION = 3
_RISK_LEVELS = ("auto", "confirm", "dual_approve", "read_only", "unknown")


class TraceSource(Protocol):
    def list_traces(self) -> list[TraceInfo]: ...


class AuditSource(Protocol):
    def read_all(self) -> list[AuditEvent]: ...


class LedgerSource(Protocol):
    def records(self) -> list[WriteRecord]: ...


@dataclass
class _Bucket:
    """单日聚合桶（内部可变）。"""

    requests: int = 0
    hallucination_count: int = 0
    fallback_count: int = 0
    guard: int = 0
    permission: int = 0
    dlp: int = 0
    risk: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_RISK_LEVELS, 0))
    cost_usd: float = 0.0
    tokens: int = 0
    llm_calls: int = 0
    write_success: int = 0
    write_failed: int = 0
    latency_sum_ms: float = 0.0

    def add(self, other: "_Bucket") -> None:
        self.requests += other.requests
        self.hallucination_count += other.hallucination_count
        self.fallback_count += other.fallback_count
        self.guard += other.guard
        self.permission += other.permission
        self.dlp += other.dlp
        for level in _RISK_LEVELS:
            self.risk[level] += other.risk[level]
        self.cost_usd += other.cost_usd
        self.tokens += other.tokens
        self.llm_calls += other.llm_calls
        self.write_success += other.write_success
        self.write_failed += other.write_failed
        self.latency_sum_ms += other.latency_sum_ms

    def to_metrics(self) -> DashboardMetrics:
        requests = self.requests
        return DashboardMetrics(
            requests=requests,
            hallucination_count=self.hallucination_count,
            hallucination_rate=_rate(self.hallucination_count, requests),
            fallback_count=self.fallback_count,
            fallback_rate=_rate(self.fallback_count, requests),
            blocked=BlockedStats(
                guard=self.guard,
                permission=self.permission,
                dlp=self.dlp,
                total=self.guard + self.permission + self.dlp,
            ),
            risk_distribution=RiskDistribution(
                **{level: self.risk.get(level, 0) for level in _RISK_LEVELS}
            ),
            cost_usd=round(self.cost_usd, _COST_PRECISION),
            tokens=self.tokens,
            llm_calls=self.llm_calls,
            write_success=self.write_success,
            write_failed=self.write_failed,
            avg_latency_ms=(
                round(self.latency_sum_ms / requests, _LATENCY_PRECISION) if requests else 0.0
            ),
        )


class DashboardService:
    """按天聚合 Trace / Audit / Ledger。"""

    def __init__(
        self,
        traces: TraceSource,
        audit: AuditSource,
        ledger: LedgerSource,
        *,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._traces = traces
        self._audit = audit
        self._ledger = ledger
        self._timezone_name = timezone
        self._tz = ZoneInfo(timezone)

    def overview(self, from_date: date, to_date: date) -> DashboardOverview:
        """生成区间总览；范围非法时抛 ValueError。"""
        if from_date > to_date:
            raise ValueError("from 不能晚于 to")
        span = (to_date - from_date).days + 1
        if span > _MAX_RANGE_DAYS:
            raise ValueError(f"查询区间不能超过 {_MAX_RANGE_DAYS} 天")

        days = [from_date + timedelta(days=index) for index in range(span)]
        buckets = {day: _Bucket() for day in days}

        for info in self._traces.list_traces():
            day = self._local_date(info.saved_at)
            bucket = buckets.get(day) if day is not None else None
            if bucket is None:
                continue
            bucket.requests += 1
            bucket.latency_sum_ms += info.total_latency_ms
            if info.hallucination_detected:
                bucket.hallucination_count += 1
            if info.used_fallback:
                bucket.fallback_count += 1
            risk = info.risk_level if info.risk_level in _RISK_LEVELS else "unknown"
            bucket.risk[risk] += 1

        for event in self._audit.read_all():
            day = self._local_date(event.timestamp)
            bucket = buckets.get(day) if day is not None else None
            if bucket is None:
                continue
            if event.event_type == "guard.blocked":
                bucket.guard += 1
            elif event.event_type == "permission.denied":
                bucket.permission += 1
            elif event.event_type == "dlp.blocked":
                bucket.dlp += 1
            elif event.event_type == "cost.recorded":
                bucket.llm_calls += 1
                bucket.cost_usd += _as_float(event.detail.get("cost_usd"))
                bucket.tokens += _as_int(event.detail.get("prompt_tokens"))
                bucket.tokens += _as_int(event.detail.get("completion_tokens"))

        for record in self._ledger.records():
            day = self._local_date(record.timestamp)
            bucket = buckets.get(day) if day is not None else None
            if bucket is None:
                continue
            if record.status == "success":
                bucket.write_success += 1
            else:
                bucket.write_failed += 1

        total = _Bucket()
        daily: list[DailyDashboard] = []
        for day in days:
            bucket = buckets[day]
            total.add(bucket)
            daily.append(DailyDashboard(date=day, **bucket.to_metrics().model_dump()))

        return DashboardOverview(
            from_date=from_date,
            to_date=to_date,
            timezone=self._timezone_name,
            totals=total.to_metrics(),
            daily=daily,
        )

    def _local_date(self, value: datetime | str) -> date | None:
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(self._tz).date()


def _rate(count: int, total: int) -> float:
    return round(count / total, _RATE_PRECISION) if total else 0.0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0
