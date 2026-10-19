"""M9-T06：DashboardService 聚合口径单测。"""

from datetime import UTC, date, datetime

import pytest

from api.app import _build_cost_sink
from governance.cost_engine import CostRecord
from governance.dashboard import DashboardService
from models.audit import AuditEvent
from models.ledger import WriteRecord, WriteStatus
from observability.audit import AuditLogger
from observability.tracing import TraceInfo

_TZ = "Asia/Shanghai"


class _ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeTraces:
    def __init__(self, items: list[TraceInfo]) -> None:
        self._items = items

    def list_traces(self) -> list[TraceInfo]:
        return list(self._items)


class FakeAudit:
    def __init__(self, items: list[AuditEvent]) -> None:
        self._items = items

    def read_all(self) -> list[AuditEvent]:
        return list(self._items)


class FakeLedger:
    def __init__(self, items: list[WriteRecord]) -> None:
        self._items = items

    def records(self) -> list[WriteRecord]:
        return list(self._items)


def _trace(
    trace_id: str,
    saved_at: datetime,
    *,
    hallucination: bool = False,
    fallback: bool = False,
    risk: str = "auto",
    latency: float = 100.0,
) -> TraceInfo:
    return TraceInfo(
        trace_id=trace_id,
        user_input="查询",
        used_fallback=fallback,
        total_latency_ms=latency,
        saved_at=saved_at.isoformat(),
        hallucination_detected=hallucination,
        risk_level=risk,
    )


def _write(timestamp: datetime, status: WriteStatus = "success") -> WriteRecord:
    return WriteRecord(
        record_id=f"record-{timestamp.isoformat()}-{status}",
        trace_id="trace-write",
        timestamp=timestamp,
        form_id="SAL_Order",
        business_key="SO000001",
        action="submit",
        status=status,
        prev_record_hash="prev",
        record_hash="hash",
    )


def _service(
    traces: list[TraceInfo],
    audit: list[AuditEvent] | None = None,
    ledger: list[WriteRecord] | None = None,
) -> DashboardService:
    return DashboardService(
        FakeTraces(traces),
        FakeAudit(audit or []),
        FakeLedger(ledger or []),
        timezone=_TZ,
    )


def test_daily_metrics_and_totals() -> None:
    service = _service(
        [
            _trace(
                "t1",
                datetime(2026, 10, 18, 1, 0, tzinfo=UTC),
                hallucination=True,
                risk="auto",
                latency=100,
            ),
            _trace(
                "t2",
                datetime(2026, 10, 18, 2, 0, tzinfo=UTC),
                fallback=True,
                risk="confirm",
                latency=300,
            ),
            _trace(
                "t3",
                datetime(2026, 10, 19, 23, 30, tzinfo=UTC),
                risk="dual_approve",
                latency=200,
            ),
        ],
        audit=[
            AuditEvent(
                event_type="guard.blocked",
                timestamp=datetime(2026, 10, 18, 3, 0, tzinfo=UTC),
            ),
            AuditEvent(
                event_type="permission.denied",
                timestamp=datetime(2026, 10, 18, 4, 0, tzinfo=UTC),
            ),
            AuditEvent(
                event_type="dlp.blocked",
                timestamp=datetime(2026, 10, 19, 23, 30, tzinfo=UTC),
            ),
            AuditEvent(
                event_type="cost.recorded",
                timestamp=datetime(2026, 10, 18, 5, 0, tzinfo=UTC),
                detail={
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "cost_usd": 0.01,
                },
            ),
            AuditEvent(
                event_type="cost.recorded",
                timestamp=datetime(2026, 10, 19, 23, 30, tzinfo=UTC),
                detail={
                    "prompt_tokens": 200,
                    "completion_tokens": 100,
                    "cost_usd": 0.02,
                },
            ),
        ],
        ledger=[
            _write(datetime(2026, 10, 18, 6, 0, tzinfo=UTC), "success"),
            _write(datetime(2026, 10, 18, 7, 0, tzinfo=UTC), "failed"),
            _write(datetime(2026, 10, 19, 23, 30, tzinfo=UTC), "success"),
        ],
    )

    overview = service.overview(date(2026, 10, 18), date(2026, 10, 20))

    assert overview.timezone == _TZ
    assert len(overview.daily) == 3
    assert overview.totals.requests == 3
    assert overview.totals.hallucination_count == 1
    assert overview.totals.hallucination_rate == 0.3333
    assert overview.totals.fallback_count == 1
    assert overview.totals.fallback_rate == 0.3333
    assert overview.totals.blocked.guard == 1
    assert overview.totals.blocked.permission == 1
    assert overview.totals.blocked.dlp == 1
    assert overview.totals.blocked.total == 3
    assert overview.totals.risk_distribution.auto == 1
    assert overview.totals.risk_distribution.confirm == 1
    assert overview.totals.risk_distribution.dual_approve == 1
    assert overview.totals.cost_usd == 0.03
    assert overview.totals.tokens == 450
    assert overview.totals.llm_calls == 2
    assert overview.totals.write_success == 2
    assert overview.totals.write_failed == 1
    assert overview.totals.avg_latency_ms == 200.0

    day_18, day_19, day_20 = overview.daily
    assert day_18.requests == 2
    assert day_18.cost_usd == 0.01
    assert day_18.write_success == 1
    assert day_18.write_failed == 1
    assert day_19.requests == 0
    assert day_19.cost_usd == 0.0
    assert day_20.requests == 1
    assert day_20.blocked.dlp == 1
    assert day_20.risk_distribution.dual_approve == 1


def test_empty_range_is_zero_filled() -> None:
    service = _service([])

    overview = service.overview(date(2026, 10, 18), date(2026, 10, 20))

    assert [item.date for item in overview.daily] == [
        date(2026, 10, 18),
        date(2026, 10, 19),
        date(2026, 10, 20),
    ]
    assert all(item.requests == 0 for item in overview.daily)
    assert overview.totals.requests == 0
    assert overview.totals.hallucination_rate == 0.0


def test_unknown_risk_is_counted() -> None:
    service = _service(
        [
            _trace(
                "unknown",
                datetime(2026, 10, 18, 1, 0, tzinfo=UTC),
                risk="not-a-level",
            )
        ]
    )

    overview = service.overview(date(2026, 10, 18), date(2026, 10, 18))

    assert overview.daily[0].requests == 1
    assert overview.daily[0].risk_distribution.unknown == 1


def test_bad_timestamp_is_skipped() -> None:
    service = _service(
        [
            TraceInfo(
                trace_id="bad",
                user_input="x",
                used_fallback=False,
                total_latency_ms=1,
                saved_at="not-a-date",
                risk_level="weird",
            )
        ]
    )

    overview = service.overview(date(2026, 10, 18), date(2026, 10, 18))

    assert overview.daily[0].requests == 0


def test_invalid_range_raises() -> None:
    service = _service([])
    with pytest.raises(ValueError):
        service.overview(date(2026, 10, 20), date(2026, 10, 18))
    with pytest.raises(ValueError):
        service.overview(date(2026, 1, 1), date(2026, 6, 1))


def test_cost_record_becomes_audit_event() -> None:
    sink = _ListSink()
    cost_sink = _build_cost_sink(AuditLogger(sink))

    cost_sink(
        CostRecord(
            route_name="chat_default",
            tier="high",
            model_id="deepseek-chat",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.000123,
            recorded_at=datetime(2026, 10, 18, 1, 0, tzinfo=UTC),
        )
    )

    event = sink.events[0]
    assert event.event_type == "cost.recorded"
    assert event.detail["prompt_tokens"] == 10
    assert event.detail["completion_tokens"] == 5
    assert event.detail["cost_usd"] == 0.000123
