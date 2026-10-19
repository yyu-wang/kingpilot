"""M6-T05：告警引擎单测。

覆盖阈值窗口、静默期、用户隔离、Saga/高风险事件、通道失败兜底
与审计流集成。
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from config.alert_rules import ALERT_RULES, AlertRule, AlertSeverity
from models.audit import AuditEvent
from observability.alerts import Alert, AlertEngine, LogAlertSink
from observability.audit import AuditLogger


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingAlertSink:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class RecordingAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FailingSink:
    def send(self, alert: Alert) -> None:
        raise RuntimeError("gateway down")


def _permission_event(user_id: str = "u001") -> AuditEvent:
    return AuditEvent(
        event_type="permission.denied",
        trace_id="trace-1",
        user_id=user_id,
        user_role="operator",
        form_id="GL_Voucher",
        status="rejected",
        detail={"domain": "finance"},
    )


def _saga_event(saga_id: str = "saga-1") -> AuditEvent:
    return AuditEvent(
        event_type="saga.failed",
        trace_id="trace-1",
        user_id="u001",
        saga_id=saga_id,
        status="failed",
    )


def _ledger_event(risk_level: str) -> AuditEvent:
    return AuditEvent(
        event_type="ledger.write",
        trace_id="trace-1",
        user_id="u001",
        business_key="SO1",
        risk_level=risk_level,
        status="success",
    )


def _engine(
    sink: RecordingAlertSink | None = None,
    clock: FakeClock | None = None,
) -> tuple[AlertEngine, RecordingAlertSink, FakeClock]:
    resolved_sink = sink or RecordingAlertSink()
    resolved_clock = clock or FakeClock()
    return (
        AlertEngine(sink=resolved_sink, clock=resolved_clock),
        resolved_sink,
        resolved_clock,
    )


# ── 阈值与窗口 ──


def test_permission_denied_threshold() -> None:
    engine, sink, _ = _engine()

    for _ in range(4):
        engine.observe(_permission_event())
    assert sink.alerts == []

    engine.observe(_permission_event())

    assert len(sink.alerts) == 1
    alert = sink.alerts[0]
    assert alert.rule_id == "PERMISSION_DENIED_FREQUENCY"
    assert alert.severity == "critical"
    assert alert.count == 5
    assert "u001" in alert.message
    assert alert.detail["domain"] == "finance"


def test_silence_window_suppresses_repeats() -> None:
    engine, sink, clock = _engine()
    for _ in range(5):
        engine.observe(_permission_event())
    assert len(sink.alerts) == 1

    for _ in range(5):
        engine.observe(_permission_event())
    assert len(sink.alerts) == 1  # 静默期内不重复告警

    clock.advance(600)
    for _ in range(5):
        engine.observe(_permission_event())
    assert len(sink.alerts) == 2


def test_window_slides_and_old_events_expire() -> None:
    engine, sink, clock = _engine()
    for _ in range(3):
        engine.observe(_permission_event())

    clock.advance(601)
    for _ in range(2):
        engine.observe(_permission_event())
    assert sink.alerts == []

    for _ in range(3):
        engine.observe(_permission_event())
    assert len(sink.alerts) == 1
    assert sink.alerts[0].count == 5


def test_keys_are_isolated_per_user() -> None:
    engine, sink, _ = _engine()
    for _ in range(3):
        engine.observe(_permission_event("u001"))
    for _ in range(4):
        engine.observe(_permission_event("u002"))
    assert sink.alerts == []

    for _ in range(2):
        engine.observe(_permission_event("u001"))

    assert len(sink.alerts) == 1
    assert sink.alerts[0].key == "u001"


# ── 其它指标 ──


def test_saga_failure_triggers_immediately() -> None:
    engine, sink, _ = _engine()

    engine.observe(_saga_event())

    assert len(sink.alerts) == 1
    assert sink.alerts[0].rule_id == "SAGA_COMPENSATION_FAILED"
    assert sink.alerts[0].key == "saga-1"


@pytest.mark.parametrize(
    ("risk_level", "expected"),
    [("read_only", 1), ("dual_approve", 1), ("auto", 0), ("confirm", 0)],
)
def test_high_risk_ledger_event(risk_level: str, expected: int) -> None:
    engine, sink, _ = _engine()

    engine.observe(_ledger_event(risk_level))

    assert len(sink.alerts) == expected
    if expected:
        assert sink.alerts[0].rule_id == "HIGH_RISK_OPERATION"
        assert sink.alerts[0].severity == "warning"


def test_unknown_event_type_is_ignored() -> None:
    engine, sink, _ = _engine()

    engine.observe(AuditEvent(event_type="alert.fired", user_id="u001"))

    assert sink.alerts == []


# ── 出口与配置 ──


def test_sink_failure_does_not_raise() -> None:
    engine = AlertEngine(sink=_FailingSink(), clock=FakeClock())

    for _ in range(5):
        engine.observe(_permission_event())

    alerts = engine.drain_alerts()
    assert len(alerts) == 1  # 告警已生成，只是发送失败


def test_alert_is_json_serializable() -> None:
    engine, sink, _ = _engine()
    engine.observe(_saga_event())

    payload = json.loads(json.dumps(sink.alerts[0].to_dict()))
    assert payload["rule_id"] == "SAGA_COMPENSATION_FAILED"
    assert payload["triggered_at"].startswith("2026-10-18T10:00:00")


def test_empty_rules_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        AlertEngine(rules=())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"threshold": 0}, "threshold"),
        ({"window_seconds": 0}, "window_seconds"),
        ({"silence_seconds": -1}, "silence_seconds"),
    ],
)
def test_rule_validation(kwargs: dict[str, int], message: str) -> None:
    base = {
        "alert_id": "X",
        "label": "x",
        "metric": "permission_denied",
        "threshold": 1,
        "window_seconds": 10,
        "severity": "warning",
        "silence_seconds": 10,
    }
    base.update(kwargs)

    with pytest.raises(ValueError, match=message):
        AlertRule(**base)  # type: ignore[arg-type]


def test_default_rules_exist() -> None:
    engine, _, _ = _engine()

    assert len(ALERT_RULES) == 5
    assert engine.rules == ALERT_RULES
    assert {rule.alert_id for rule in ALERT_RULES} == {
        "PERMISSION_DENIED_FREQUENCY",
        "SAGA_COMPENSATION_FAILED",
        "HIGH_RISK_OPERATION",
        "BUDGET_DOWNGRADED",
        "BUDGET_EXHAUSTED",
    }


@pytest.mark.parametrize("severity", ["critical", "warning", "info"])
def test_log_alert_sink_handles_all_severities(severity: AlertSeverity) -> None:
    alert = Alert(
        alert_id="a1",
        rule_id="RULE",
        label="测试",
        severity=severity,
        channel="log",
        key="u001",
        count=1,
        message="测试告警",
        triggered_at=FakeClock()(),
    )

    LogAlertSink().send(alert)  # 不抛异常即可


# ── 审计流集成 ──


def test_audit_logger_subscription_triggers_alert() -> None:
    audit_sink = RecordingAuditSink()
    alert_sink = RecordingAlertSink()
    audit = AuditLogger(audit_sink)
    engine = AlertEngine(sink=alert_sink, clock=FakeClock())
    audit.subscribe(engine.observe)

    for _ in range(5):
        audit.emit(_permission_event())

    assert len(audit_sink.events) == 5
    assert len(alert_sink.alerts) == 1
    assert alert_sink.alerts[0].rule_id == "PERMISSION_DENIED_FREQUENCY"
