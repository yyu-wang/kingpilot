"""M6-T05 · 告警规则表（方案 §5.2 支柱七）。

运行时只读。规则声明式：指标 + 阈值 + 窗口 + 静默期 + 通道。
通道先实现 log；im/sms/phone 仅保留配置字段，后续接通知网关。
"""

from dataclasses import dataclass
from typing import Literal

AlertMetric = Literal[
    "permission_denied",
    "saga_compensation_failed",
    "high_risk",
    "budget_downgraded",
    "budget_exhausted",
]
AlertSeverity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class AlertRule:
    """一条告警规则。"""

    alert_id: str
    label: str
    metric: AlertMetric
    threshold: int
    window_seconds: int
    severity: AlertSeverity
    silence_seconds: int
    channel: str = "log"

    def __post_init__(self) -> None:
        if self.threshold <= 0:
            raise ValueError(f"告警规则 {self.alert_id} 的 threshold 必须大于 0")
        if self.window_seconds <= 0:
            raise ValueError(f"告警规则 {self.alert_id} 的 window_seconds 必须大于 0")
        if self.silence_seconds < 0:
            raise ValueError(f"告警规则 {self.alert_id} 的 silence_seconds 不能为负")


ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        alert_id="PERMISSION_DENIED_FREQUENCY",
        label="高频越权尝试",
        metric="permission_denied",
        threshold=5,
        window_seconds=600,
        severity="critical",
        silence_seconds=600,
    ),
    AlertRule(
        alert_id="SAGA_COMPENSATION_FAILED",
        label="Saga 补偿失败",
        metric="saga_compensation_failed",
        threshold=1,
        window_seconds=300,
        severity="critical",
        silence_seconds=300,
    ),
    AlertRule(
        alert_id="HIGH_RISK_OPERATION",
        label="高风险写操作",
        metric="high_risk",
        threshold=1,
        window_seconds=60,
        severity="warning",
        silence_seconds=60,
    ),
    # M7-T04：预算降档 / 耗尽（进程级成本，按用户聚合）
    AlertRule(
        alert_id="BUDGET_DOWNGRADED",
        label="模型预算降档",
        metric="budget_downgraded",
        threshold=1,
        window_seconds=300,
        severity="warning",
        silence_seconds=300,
    ),
    AlertRule(
        alert_id="BUDGET_EXHAUSTED",
        label="模型预算耗尽",
        metric="budget_exhausted",
        threshold=1,
        window_seconds=300,
        severity="critical",
        silence_seconds=300,
    ),
)
