"""支柱七/十/十二 · 告警规则表（占位，M7 填充）。

对应方案 §17 支柱七（可观测）、支柱十（成本）、支柱十二（异常自愈）：
何时、以何种阈值、向何处触发告警，运行时**只读**。

规则字段（M7 定稿，此处仅占位）：
    alert_id      告警唯一标识
    metric        监控指标（latency / error_rate / token_cost / ...）
    threshold     阈值表达式
    channel       通知渠道（log / webhook / lark）
    severity      级别（info/warning/critical）

M7 之前保持空表：观测仅落结构化日志，不产生外部告警。
"""

from typing import Any

ALERT_RULES: list[dict[str, Any]] = []
