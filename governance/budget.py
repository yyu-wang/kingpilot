"""预算熔断策略（M7-T04，方案 §5.4 支柱十二）。

纯函数式决策，不持有成本账本、不发审计——便于单测与复用：
- 花费达 ``downgrade_ratio``：high → mid
- 花费达 100%：high/mid → economy
- economy 仍达 100%：拒绝调用（由 RoutingProvider 抛 BudgetExceededError）
"""

from dataclasses import dataclass
from typing import Literal

from config.model_registry import ModelTier

BudgetAction = Literal["keep", "downgrade", "exhausted"]


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """一次预算检查的结果。"""

    action: BudgetAction
    tier: ModelTier
    spent_usd: float


class BudgetPolicy:
    """进程级预算阈值（金额为 USD）。"""

    def __init__(self, budget_usd: float, *, downgrade_ratio: float = 0.8) -> None:
        if budget_usd <= 0:
            raise ValueError("预算必须大于 0")
        if not 0.0 < downgrade_ratio < 1.0:
            raise ValueError("降档比例必须在 (0, 1) 区间")
        self._budget_usd = budget_usd
        self._downgrade_ratio = downgrade_ratio

    @property
    def budget_usd(self) -> float:
        return self._budget_usd

    @property
    def downgrade_ratio(self) -> float:
        return self._downgrade_ratio

    def evaluate(self, spent_usd: float, tier: ModelTier) -> BudgetDecision:
        """按当前花费给出 keep / downgrade / exhausted。"""
        if tier == "economy":
            if spent_usd >= self._budget_usd:
                return BudgetDecision(action="exhausted", tier="economy", spent_usd=spent_usd)
            return BudgetDecision(action="keep", tier="economy", spent_usd=spent_usd)

        if spent_usd >= self._budget_usd:
            return BudgetDecision(action="downgrade", tier="economy", spent_usd=spent_usd)

        if tier == "high" and spent_usd >= self._budget_usd * self._downgrade_ratio:
            return BudgetDecision(action="downgrade", tier="mid", spent_usd=spent_usd)

        return BudgetDecision(action="keep", tier=tier, spent_usd=spent_usd)
