"""风险评分引擎（M4-T07，方案 §5.1 支柱一）。

职责：把一次工具调用的上下文（工具类型/动作/金额/批量/用户画像/时间）
换算成 0~100 风险分，并按四档阈值路由 auto / confirm / dual_approve / read_only。

设计要点：
- **规则与逻辑分离**：规则参数在 ``config/risk_rules.py``，本模块只负责分发计算。
- **明细可审计**：``break_down`` 保留全部规则（含未触发项）的原始分、权重与明细。
- **fail-closed**：空规则表/空档位表在构造期拒绝；未知规则类型直接报错。
- 本模块不执行拦截，只产出决策；HITL 确认交互归 T09。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.tools import ToolDefinition, ToolType
from config.risk_rules import (
    BUSINESS_HOURS_END,
    BUSINESS_HOURS_START,
    BUSINESS_WEEKDAYS,
    RISK_RULES,
    RISK_TIERS,
    SENSITIVE_OBJECTS,
    ApprovalLevel,
    RiskRule,
    RiskTier,
)
from models.user import UserContext

_MAX_SCORE = 100.0
_DEFAULT_BATCH_SIZE = 1
# 查询工具没有批量语义，默认 0 避免给只读查询加无意义的批量分
_QUERY_BATCH_SIZE = 0


@dataclass(frozen=True)
class RiskContext:
    """一次工具调用的风险评分输入（方案 §5.1 RiskContext 的落地形状）。"""

    tool_type: ToolType
    action: str
    form_id: str
    amount: float = 0.0
    batch_size: int = 1
    user_role: str = ""
    user_risk_score: float = 0.0
    is_business_hours: bool = True


@dataclass(frozen=True)
class ScoreItem:
    """单条规则得分明细（审计用）。"""

    rule_id: str
    label: str
    raw_score: float  # 已按规则 max_points 截断
    weight: float
    score: float  # raw_score × weight
    triggered: bool
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "rule_id": self.rule_id,
            "label": self.label,
            "raw_score": self.raw_score,
            "weight": self.weight,
            "score": self.score,
            "triggered": self.triggered,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class RiskDecision:
    """风险评估结果：总分 + 四档路由 + 明细。"""

    score: float
    level: ApprovalLevel
    break_down: tuple[ScoreItem, ...]
    required_approvers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转 ``AgentTrace.risk_assessment`` 的字典形状。"""
        return {
            "score": self.score,
            "level": self.level,
            "required_approvers": list(self.required_approvers),
            "break_down": [item.to_dict() for item in self.break_down],
        }


class RiskEngine:
    """规则表驱动的风险评分与分档路由。"""

    def __init__(
        self,
        rules: Sequence[RiskRule] = RISK_RULES,
        tiers: Sequence[RiskTier] = RISK_TIERS,
    ) -> None:
        """装配引擎。

        Raises:
            ValueError: 规则表或档位表为空（无规则 = 无法评估 = 拒绝）。
        """
        if not rules:
            raise ValueError("风险规则表不能为空（fail-closed）")
        if not tiers:
            raise ValueError("风险档位表不能为空（fail-closed）")
        self._rules = tuple(rules)
        self._tiers = tuple(sorted(tiers, key=lambda tier: tier.max_score))

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """规则 ID 顺序（自检/审计用）。"""
        return tuple(rule.rule_id for rule in self._rules)

    def assess(self, context: RiskContext) -> RiskDecision:
        """计算风险分并路由档位；总分截断到 0~100。"""
        items = tuple(_evaluate_rule(rule, context) for rule in self._rules)
        total = min(sum(item.score for item in items), _MAX_SCORE)
        return RiskDecision(score=total, level=self.route(total), break_down=items)

    def route(self, score: float) -> ApprovalLevel:
        """按档位上限路由；超出全部阈值时落最后一档（read_only）。"""
        for tier in self._tiers:
            if score <= tier.max_score:
                return tier.level
        return self._tiers[-1].level


def build_risk_context(
    tool: ToolDefinition,
    arguments: Mapping[str, Any],
    user_context: UserContext,
    *,
    now: datetime | None = None,
) -> RiskContext:
    """从工具元数据 + LLM 参数 + 用户上下文组装评分输入。

    约定：写工具用 ``amount``（元）/ ``batch_size``（条数）参数；
    缺省金额 0、批量 1（查询工具批量 0）。类型非法时保守按缺省值处理
    （参数形状由 Guard 1 负责拦截）。
    """
    default_batch = _QUERY_BATCH_SIZE if tool.tool_type == "query" else _DEFAULT_BATCH_SIZE
    return RiskContext(
        tool_type=tool.tool_type,
        action=tool.action,
        form_id=tool.endpoint.form_id,
        amount=_as_float(arguments.get("amount")),
        batch_size=_as_batch_size(arguments.get("batch_size"), default=default_batch),
        user_role=user_context.role,
        user_risk_score=user_context.risk_score,
        is_business_hours=is_business_hours(now),
    )


def is_business_hours(now: datetime | None = None) -> bool:
    """是否工作时间：周一~周五 09:00-18:00（左闭右开，本地时区）。"""
    current = now or datetime.now()
    if current.weekday() not in BUSINESS_WEEKDAYS:
        return False
    return BUSINESS_HOURS_START <= current.time() < BUSINESS_HOURS_END


def _evaluate_rule(rule: RiskRule, context: RiskContext) -> ScoreItem:
    """单条规则 → 得分明细。"""
    raw_score, triggered, detail = _raw_score(rule, context)
    raw_score = min(max(raw_score, 0.0), rule.max_points)
    return ScoreItem(
        rule_id=rule.rule_id,
        label=rule.label,
        raw_score=raw_score,
        weight=rule.weight,
        score=raw_score * rule.weight,
        triggered=triggered,
        detail=detail,
    )


def _raw_score(rule: RiskRule, context: RiskContext) -> tuple[float, bool, dict[str, Any]]:
    """按规则类型计算原始分（未加权）。"""
    if rule.kind == "write_base":
        triggered = context.tool_type == "write"
        return (rule.points if triggered else 0.0, triggered, {"tool_type": context.tool_type})
    if rule.kind == "amount":
        triggered = context.amount > 0
        raw = context.amount / rule.unit * rule.per_unit_points if triggered else 0.0
        return raw, triggered, {"amount": context.amount}
    if rule.kind == "action":
        triggered = bool(context.action) and context.action in rule.actions
        return (rule.points if triggered else 0.0, triggered, {"action": context.action})
    if rule.kind == "batch":
        triggered = context.batch_size > 0
        raw = context.batch_size / rule.unit * rule.per_unit_points if triggered else 0.0
        return raw, triggered, {"batch_size": context.batch_size}
    if rule.kind == "sensitive":
        triggered = context.form_id in SENSITIVE_OBJECTS
        return (
            rule.points if triggered else 0.0,
            triggered,
            {"form_id": context.form_id},
        )
    if rule.kind == "user_risk":
        triggered = context.user_risk_score > 0
        return (
            context.user_risk_score if triggered else 0.0,
            triggered,
            {"user_risk_score": context.user_risk_score},
        )
    if rule.kind == "off_hours":
        triggered = not context.is_business_hours
        return (
            rule.points if triggered else 0.0,
            triggered,
            {"is_business_hours": context.is_business_hours},
        )
    raise ValueError(f"未知风险规则类型: {rule.kind}")


def _as_float(value: Any) -> float:
    """金额提取：数值或数字字符串；其它类型按 0（Guard 1 负责拦非法形状）。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _as_batch_size(value: Any, *, default: int) -> int:
    """批量条数提取；负数按 0。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and value.is_integer():
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value.strip()), 0)
        except ValueError:
            return default
    return default
