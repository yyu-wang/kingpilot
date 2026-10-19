"""发布门禁规则表（M8-T03）。

阈值与方向集中在此处，逻辑层只做比较：
- ``min``：越高越好（准确率），低于绝对下限或相对基线退化超容忍 → 阻断
- ``max``：越低越好（错误率/延迟/成本），高于绝对上限或相对基线上升超容忍 → 阻断

初始阈值为工程默认值，可按线上账单与 SLA 调整；调门槛不改代码。
"""

from dataclasses import dataclass
from typing import Literal

MetricDirection = Literal["min", "max"]


@dataclass(frozen=True)
class GateRule:
    """一条门禁规则。"""

    metric: str
    direction: MetricDirection
    floor: float | None = None
    ceiling: float | None = None
    max_regression: float = 0.0

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise ValueError("门禁规则的 metric 不能为空")
        if self.max_regression < 0:
            raise ValueError(f"门禁规则 {self.metric} 的 max_regression 不能为负")
        if self.direction == "min":
            if self.floor is None:
                raise ValueError(f"min 指标 {self.metric} 必须配置 floor")
            if self.ceiling is not None:
                raise ValueError(f"min 指标 {self.metric} 不能配置 ceiling")
        else:
            if self.ceiling is None:
                raise ValueError(f"max 指标 {self.metric} 必须配置 ceiling")
            if self.floor is not None:
                raise ValueError(f"max 指标 {self.metric} 不能配置 floor")


#: 门禁指标（与 M8-T01 report.metrics 对齐）
GATE_RULES: tuple[GateRule, ...] = (
    GateRule("tool_selection_accuracy", "min", floor=0.90, max_regression=0.02),
    GateRule("error_rate", "max", ceiling=0.10, max_regression=0.02),
    GateRule("avg_latency_ms", "max", ceiling=5000.0, max_regression=500.0),
    GateRule("avg_cost_usd", "max", ceiling=0.05, max_regression=0.005),
)
