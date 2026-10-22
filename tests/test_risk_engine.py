"""M4-T07：风险评分引擎单测。

覆盖八条规则、四档边界（20/50/80）、总分截断、审计明细、
配置驱动与上下文构造。
"""

import json
from datetime import datetime
from typing import Any

import pytest
from pydantic import Field

from agent.tools import ToolDefinition, ToolType
from config.risk_rules import RiskRule, RiskTier
from erp import FilterExpression
from kingdee.endpoints.base import EndpointParams
from kingdee.endpoints.sal_order import SAL_ORDER
from models.base import KingPilotBaseModel
from models.llm import ToolSpec
from models.user import UserContext
from safety.risk_engine import (
    RiskContext,
    RiskDecision,
    RiskEngine,
    ScoreItem,
    build_risk_context,
    is_business_hours,
)


def _context(
    *,
    tool_type: ToolType = "write",
    action: str = "save",
    form_id: str = "SAL_Order",
    amount: float = 0.0,
    batch_size: int = 1,
    user_role: str = "operator",
    user_risk_score: float = 0.0,
    is_business_hours: bool = True,
) -> RiskContext:
    return RiskContext(
        tool_type=tool_type,
        action=action,
        form_id=form_id,
        amount=amount,
        batch_size=batch_size,
        user_role=user_role,
        user_risk_score=user_risk_score,
        is_business_hours=is_business_hours,
    )


def _item(decision: RiskDecision, rule_id: str) -> ScoreItem:
    return next(item for item in decision.break_down if item.rule_id == rule_id)


# ── 八条规则 ──


def test_write_base_rule() -> None:
    engine = RiskEngine()

    assert _item(engine.assess(_context()), "write_base").score == 30
    query = _context(tool_type="query", action="", batch_size=0)
    assert _item(engine.assess(query), "write_base").score == 0


@pytest.mark.parametrize(
    ("amount", "raw_score"),
    [(0, 0.0), (10_000, 1.0), (25_000, 2.5), (300_000, 30.0), (1_000_000, 30.0)],
)
def test_amount_rule(amount: float, raw_score: float) -> None:
    engine = RiskEngine()

    item = _item(engine.assess(_context(amount=amount)), "amount")

    assert item.raw_score == pytest.approx(raw_score)
    assert item.triggered is (amount > 0)


def test_action_rules() -> None:
    engine = RiskEngine()

    assert _item(engine.assess(_context(action="audit")), "audit").score == 25
    assert _item(engine.assess(_context(action="unaudit")), "audit").score == 25
    assert _item(engine.assess(_context(action="save")), "audit").score == 0
    assert _item(engine.assess(_context(action="delete")), "delete").score == 40
    assert _item(engine.assess(_context(action="save")), "delete").score == 0


@pytest.mark.parametrize(
    ("batch_size", "raw_score"),
    [(0, 0.0), (10, 1.0), (100, 10.0), (500, 20.0)],
)
def test_batch_rule(batch_size: int, raw_score: float) -> None:
    engine = RiskEngine()

    item = _item(engine.assess(_context(batch_size=batch_size)), "batch")

    assert item.raw_score == pytest.approx(raw_score)


def test_sensitive_object_rule() -> None:
    engine = RiskEngine()

    assert _item(engine.assess(_context(form_id="GL_Voucher")), "sensitive_object").score == 20
    assert _item(engine.assess(_context(form_id="SAL_Order")), "sensitive_object").score == 0


def test_user_risk_rule_weighted() -> None:
    engine = RiskEngine()

    item = _item(engine.assess(_context(user_risk_score=40)), "user_risk")
    assert item.raw_score == 40
    assert item.score == pytest.approx(20)  # 40 × 0.5
    assert _item(engine.assess(_context(user_risk_score=0)), "user_risk").score == 0


def test_off_hours_rule_weighted() -> None:
    engine = RiskEngine()

    item = _item(engine.assess(_context(is_business_hours=False)), "off_hours")
    assert item.raw_score == 10
    assert item.score == pytest.approx(3)  # 10 × 0.3
    assert _item(engine.assess(_context(is_business_hours=True)), "off_hours").score == 0


# ── 分档边界 ──


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (0, "auto"),
        (20, "auto"),
        (20.5, "confirm"),
        (50, "confirm"),
        (50.5, "dual_approve"),
        (80, "dual_approve"),
        (80.5, "read_only"),
        (100, "read_only"),
        (150, "read_only"),
    ],
)
def test_route_boundaries(score: float, level: str) -> None:
    assert RiskEngine().route(score) == level


def test_high_risk_scenario_clamped_to_100() -> None:
    decision = RiskEngine().assess(
        _context(
            action="delete",
            form_id="GL_Voucher",
            amount=1_000_000,
            batch_size=500,
            user_risk_score=100,
            is_business_hours=False,
        )
    )

    assert decision.score == 100
    assert decision.level == "read_only"


# ── 审计明细与配置驱动 ──


def test_break_down_contains_all_rules_and_is_serializable() -> None:
    engine = RiskEngine()

    decision = engine.assess(_context(amount=25_000))

    assert tuple(item.rule_id for item in decision.break_down) == engine.rule_ids
    payload = json.loads(json.dumps(decision.to_dict()))
    assert payload["score"] == decision.score
    assert payload["level"] == decision.level
    assert payload["required_approvers"] == []
    assert len(payload["break_down"]) == 8


def test_custom_rules_and_tiers_are_config_driven() -> None:
    engine = RiskEngine(
        rules=(RiskRule(rule_id="tiny", label="测试规则", kind="write_base", points=5),),
        tiers=(
            RiskTier(level="read_only", max_score=100),
            RiskTier(level="auto", max_score=1),
        ),
    )

    decision = engine.assess(_context())

    assert decision.score == 5
    assert decision.level == "read_only"


def test_empty_rules_or_tiers_rejected() -> None:
    with pytest.raises(ValueError, match="规则表不能为空"):
        RiskEngine(rules=(), tiers=(RiskTier(level="auto", max_score=100),))
    with pytest.raises(ValueError, match="档位表不能为空"):
        RiskEngine(rules=(RiskRule(rule_id="r", label="r", kind="write_base"),), tiers=())


def test_rule_parameter_validation() -> None:
    with pytest.raises(ValueError, match="unit"):
        RiskRule(rule_id="bad", label="bad", kind="amount", unit=0)
    with pytest.raises(ValueError, match="weight"):
        RiskRule(rule_id="bad", label="bad", kind="write_base", weight=-1)


def test_unknown_rule_kind_fails_closed() -> None:
    bad = RiskRule(rule_id="bad", label="bad", kind="bogus")  # type: ignore[arg-type]
    engine = RiskEngine(
        rules=(bad,),
        tiers=(RiskTier(level="read_only", max_score=100),),
    )

    with pytest.raises(ValueError, match="未知风险规则类型"):
        engine.assess(_context())


# ── 上下文构造 ──


class _WriteParams(EndpointParams):
    amount: float = Field(default=0.0, description="金额（元）")
    batch_size: int = Field(default=1, description="批量条数")


def _empty_filter(params: KingPilotBaseModel) -> FilterExpression:
    return FilterExpression()


def _tool(*, tool_type: ToolType, action: str) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name="erp_create_order",
            description="测试用写工具",
            parameters=_WriteParams.model_json_schema(),
        ),
        params_model=_WriteParams,
        build_filter=_empty_filter,
        endpoint=SAL_ORDER,
        tool_type=tool_type,
        action=action,
    )


def test_build_risk_context_from_tool_arguments_and_user() -> None:
    user = UserContext(user_id="u001", role="operator", risk_score=40)

    context = build_risk_context(
        _tool(tool_type="write", action="save"),
        {"amount": "25000", "batch_size": "12"},
        user,
        now=datetime(2026, 10, 19, 10, 0),
    )

    assert context.tool_type == "write"
    assert context.action == "save"
    assert context.form_id == "SAL_Order"
    assert context.amount == 25_000
    assert context.batch_size == 12
    assert context.user_role == "operator"
    assert context.user_risk_score == 40
    assert context.is_business_hours is True


def test_query_tool_has_no_batch_risk() -> None:
    user = UserContext(user_id="u001", role="viewer")

    context = build_risk_context(
        _tool(tool_type="query", action=""),
        {},
        user,
        now=datetime(2026, 10, 19, 10, 0),
    )

    assert context.batch_size == 0
    assert RiskEngine().assess(context).score == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, 0.0), (25_000, 25_000.0), ("abc", 0.0), ([], 0.0)],
)
def test_build_risk_context_amount_parsing(value: Any, expected: float) -> None:
    user = UserContext(user_id="u001", role="operator")

    context = build_risk_context(
        _tool(tool_type="write", action="save"),
        {"amount": value},
        user,
        now=datetime(2026, 10, 19, 10, 0),
    )

    assert context.amount == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, 1), (12, 12), (12.0, 12), ("abc", 1), ([], 1), (-5, 0)],
)
def test_build_risk_context_batch_parsing(value: Any, expected: int) -> None:
    user = UserContext(user_id="u001", role="operator")

    context = build_risk_context(
        _tool(tool_type="write", action="save"),
        {"batch_size": value},
        user,
        now=datetime(2026, 10, 19, 10, 0),
    )

    assert context.batch_size == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 10, 19, 10, 0), True),  # 周一 10:00
        (datetime(2026, 10, 19, 8, 59), False),
        (datetime(2026, 10, 19, 18, 0), False),  # 右开：18:00 不算
        (datetime(2026, 10, 24, 10, 0), False),  # 周六
    ],
)
def test_is_business_hours(moment: datetime, expected: bool) -> None:
    assert is_business_hours(moment) is expected
