"""告警引擎（M6-T05，方案 §5.2 支柱七）。

输入：``AuditEvent``（M6-T04 审计流）。
处理：事件 → 指标 → 滑动窗口计数 → 规则匹配 → 触发告警。
去重：同一 ``(rule_id, key)`` 在静默期内只告警一次，防告警风暴。

通道：``LogAlertSink`` 先落地结构化日志；im/sms/phone 由规则配置预留，
后续接通知网关时只需新增 ``AlertSink`` 实现。
"""

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from loguru import logger

from config.alert_rules import (
    ALERT_RULES,
    AlertMetric,
    AlertRule,
    AlertSeverity,
)
from models.audit import AuditEvent

# 触发高风险告警的写操作风险档位
_HIGH_RISK_LEVELS = frozenset({"dual_approve", "read_only"})


@dataclass(frozen=True)
class Alert:
    """一条已触发的告警。"""

    alert_id: str
    rule_id: str
    label: str
    severity: AlertSeverity
    channel: str
    key: str
    count: int
    message: str
    triggered_at: datetime
    trace_id: str = ""
    user_id: str = ""
    form_id: str = ""
    business_key: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "alert_id": self.alert_id,
            "rule_id": self.rule_id,
            "label": self.label,
            "severity": self.severity,
            "channel": self.channel,
            "key": self.key,
            "count": self.count,
            "message": self.message,
            "triggered_at": self.triggered_at.isoformat(),
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "form_id": self.form_id,
            "business_key": self.business_key,
            "detail": dict(self.detail),
        }


class AlertSink(Protocol):
    """告警发送通道。"""

    def send(self, alert: Alert) -> None:
        """发送告警；失败可抛异常，由引擎统一告警。"""
        ...


class LogAlertSink:
    """结构化日志通道（本地可用；IM/SMS 后续实现同协议）。"""

    def send(self, alert: Alert) -> None:
        message = (
            "ALERT | rule={} severity={} channel={} key={} count={} message={} trace_id={} user={}"
        )
        args = (
            alert.rule_id,
            alert.severity,
            alert.channel,
            alert.key,
            alert.count,
            alert.message,
            alert.trace_id,
            alert.user_id,
        )
        if alert.severity == "critical":
            logger.critical(message, *args)
        elif alert.severity == "warning":
            logger.warning(message, *args)
        else:
            logger.info(message, *args)


class AlertEngine:
    """规则表驱动的告警引擎（实现 AuditObserver 签名）。"""

    def __init__(
        self,
        rules: Sequence[AlertRule] = ALERT_RULES,
        *,
        sink: AlertSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not rules:
            raise ValueError("告警规则不能为空（fail-closed）")
        self._rules = tuple(rules)
        self._sink = sink or LogAlertSink()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._events: dict[tuple[str, str], deque[datetime]] = {}
        self._silenced_until: dict[tuple[str, str], datetime] = {}
        self._fired: list[Alert] = []
        # 同一指标可能有不同窗口的规则，保留最大窗口用于剪枝
        self._max_window: dict[str, int] = {}
        for rule in self._rules:
            self._max_window[rule.metric] = max(
                self._max_window.get(rule.metric, 0), rule.window_seconds
            )

    def observe(self, event: AuditEvent) -> None:
        """消费一条审计事件；满足规则时触发告警。"""
        mapped = _metric_for_event(event)
        if mapped is None:
            return
        metric, key = mapped
        now = self._clock()
        bucket = self._events.setdefault((metric, key), deque())
        bucket.append(now)
        self._prune(bucket, metric, now)

        for rule in self._rules:
            if rule.metric != metric:
                continue
            if self._is_silenced(rule, key, now):
                continue
            count = sum(
                1 for moment in bucket if moment > now - timedelta(seconds=rule.window_seconds)
            )
            if count < rule.threshold:
                continue

            alert = _build_alert(rule, key, count, event, now)
            self._fired.append(alert)
            self._silenced_until[(rule.alert_id, key)] = now + timedelta(
                seconds=rule.silence_seconds
            )
            self._send(alert)

    def drain_alerts(self) -> list[Alert]:
        """取走已触发告警（测试/通知消费者用）。"""
        alerts = list(self._fired)
        self._fired.clear()
        return alerts

    @property
    def rules(self) -> tuple[AlertRule, ...]:
        return self._rules

    def _prune(self, bucket: deque[datetime], metric: str, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._max_window.get(metric, 0))
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def _is_silenced(self, rule: AlertRule, key: str, now: datetime) -> bool:
        until = self._silenced_until.get((rule.alert_id, key))
        return until is not None and now < until

    def _send(self, alert: Alert) -> None:
        try:
            self._sink.send(alert)
        except Exception as exc:
            logger.critical(
                "告警发送失败 | rule={} key={} error={}",
                alert.rule_id,
                alert.key,
                exc,
            )


def _metric_for_event(event: AuditEvent) -> tuple[AlertMetric, str] | None:
    """审计事件 → (指标, 聚合 key)。"""
    if event.event_type == "permission.denied":
        return "permission_denied", event.user_id or event.trace_id or "-"
    if event.event_type == "saga.failed":
        return "saga_compensation_failed", event.saga_id or event.trace_id or "-"
    if event.event_type == "ledger.write" and event.risk_level in _HIGH_RISK_LEVELS:
        return "high_risk", event.user_id or event.trace_id or "-"
    return None


def _build_alert(
    rule: AlertRule,
    key: str,
    count: int,
    event: AuditEvent,
    now: datetime,
) -> Alert:
    return Alert(
        alert_id=uuid4().hex,
        rule_id=rule.alert_id,
        label=rule.label,
        severity=rule.severity,
        channel=rule.channel,
        key=key,
        count=count,
        message=f"{rule.label}: {key} 在 {rule.window_seconds} 秒内触发 {count} 次",
        triggered_at=now,
        trace_id=event.trace_id,
        user_id=event.user_id,
        form_id=event.form_id,
        business_key=event.business_key,
        detail=dict(event.detail),
    )
