"""M4-T09：HITL 四档路由与确认等待单测。

覆盖四档路由、权限升级/拒绝、confirm/dual 同意与拒绝、
read_only 不执行、CLI IO 解析与摘要生成。
"""

import json

import pytest

from config.risk_rules import ApprovalLevel
from models.llm import ToolCallDecision
from safety.human_in_loop import (
    CliConfirmationIO,
    HitlDecision,
    HitlOutcome,
    HumanInTheLoop,
    build_action_summary,
)
from safety.permission_engine import PermissionDecision
from safety.risk_engine import RiskDecision


class FakeIO:
    """按顺序返回预设答案的确认接口。"""

    def __init__(self, answers: list[bool] | None = None) -> None:
        self._answers = list(answers or [])
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else False


def _risk(
    level: ApprovalLevel,
    score: float = 0.0,
    approvers: tuple[str, ...] = (),
) -> RiskDecision:
    return RiskDecision(
        score=score,
        level=level,
        break_down=(),
        required_approvers=approvers,
    )


def _permission(
    *,
    allowed: bool = True,
    approvers: tuple[str, ...] = (),
    reason: str = "",
) -> PermissionDecision:
    return PermissionDecision(
        allowed=allowed,
        reason=reason,
        required_approvers=approvers,
    )


# ── 路由 ──


def test_route_auto() -> None:
    decision = HumanInTheLoop(FakeIO()).route(_risk("auto", 5))

    assert decision.level == "auto"
    assert decision.blocked is False
    assert "5.0" in decision.reason


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("confirm", "confirm"),
        ("dual_approve", "dual_approve"),
        ("read_only", "read_only"),
    ],
)
def test_route_preserves_risk_level(level: ApprovalLevel, expected: str) -> None:
    decision = HumanInTheLoop(FakeIO()).route(_risk(level, 60))

    assert decision.level == expected


def test_permission_escalation_raises_to_dual_approve() -> None:
    decision = HumanInTheLoop(FakeIO()).route(
        _risk("auto", 10), _permission(approvers=("sales_manager",), reason="超限")
    )

    assert decision.level == "dual_approve"
    assert decision.approvers == ("sales_manager",)
    assert "超限" in decision.reason


def test_read_only_is_not_downgraded_by_permission() -> None:
    decision = HumanInTheLoop(FakeIO()).route(
        _risk("read_only", 90), _permission(approvers=("sales_manager",))
    )

    assert decision.level == "read_only"


def test_permission_denial_blocks() -> None:
    decision = HumanInTheLoop(FakeIO()).route(
        _risk("auto"), _permission(allowed=False, reason="暂无财务权限")
    )

    assert decision.blocked is True
    assert decision.level == "read_only"
    assert decision.reason == "暂无财务权限"


def test_approvers_are_deduped_and_ordered() -> None:
    decision = HumanInTheLoop(FakeIO()).route(
        _risk("dual_approve", approvers=("manager",)),
        _permission(approvers=("manager", "director")),
    )

    assert decision.approvers == ("manager", "director")


# ── 确认等待 ──


async def test_auto_does_not_prompt() -> None:
    io = FakeIO([False])
    hitl = HumanInTheLoop(io)

    outcome = await hitl.confirm(hitl.route(_risk("auto")))

    assert outcome.approved is True
    assert io.prompts == []


async def test_confirm_accepted() -> None:
    io = FakeIO([True])
    hitl = HumanInTheLoop(io)
    decision = hitl.route(_risk("confirm", 35))

    outcome = await hitl.confirm(decision, "即将创建销售订单")

    assert outcome.approved is True
    assert outcome.level == "confirm"
    assert outcome.message == "已确认执行。"
    assert len(io.prompts) == 1
    assert "即将创建销售订单" in io.prompts[0]
    assert "确认执行" in io.prompts[0]


async def test_confirm_rejected_cancels() -> None:
    io = FakeIO([False])
    hitl = HumanInTheLoop(io)

    outcome = await hitl.confirm(hitl.route(_risk("confirm", 35)))

    assert outcome.approved is False
    assert outcome.cancelled is True
    assert outcome.message == "已取消本次操作。"


async def test_dual_approve_both_accept() -> None:
    io = FakeIO([True, True])
    hitl = HumanInTheLoop(io)
    decision = hitl.route(_risk("dual_approve", 70, approvers=("manager",)))

    outcome = await hitl.confirm(decision, "即将删除单据")

    assert outcome.approved is True
    assert outcome.level == "dual_approve"
    assert len(io.prompts) == 2
    assert io.prompts[0].startswith("【第一确认人】")
    assert "【复核人（manager）】" in io.prompts[1]


async def test_dual_approve_first_rejects() -> None:
    io = FakeIO([False])
    hitl = HumanInTheLoop(io)

    outcome = await hitl.confirm(hitl.route(_risk("dual_approve", 70)))

    assert outcome.approved is False
    assert outcome.cancelled is True
    assert "第一确认人取消" in outcome.message
    assert len(io.prompts) == 1


async def test_dual_approve_second_rejects() -> None:
    io = FakeIO([True, False])
    hitl = HumanInTheLoop(io)

    outcome = await hitl.confirm(hitl.route(_risk("dual_approve", 70)))

    assert outcome.approved is False
    assert outcome.cancelled is True
    assert "复核人取消" in outcome.message
    assert len(io.prompts) == 2


async def test_read_only_does_not_prompt() -> None:
    io = FakeIO([True])
    hitl = HumanInTheLoop(io)

    outcome = await hitl.confirm(hitl.route(_risk("read_only", 90)))

    assert outcome.approved is False
    assert outcome.read_only is True
    assert "人工执行" in outcome.message
    assert io.prompts == []


async def test_blocked_permission_does_not_prompt() -> None:
    io = FakeIO([True])
    hitl = HumanInTheLoop(io)
    decision = hitl.route(_risk("auto"), _permission(allowed=False, reason="暂无财务权限"))

    outcome = await hitl.confirm(decision)

    assert outcome.approved is False
    assert outcome.blocked is True
    assert outcome.message == "暂无财务权限"
    assert io.prompts == []


# ── 摘要与 CLI IO ──


def test_build_action_summary() -> None:
    calls = [
        ToolCallDecision(
            tool_name="erp_create_sales_order",
            arguments={"customer_number": "CUS001", "quantity": 20.0, "note": ""},
        ),
        ToolCallDecision(
            tool_name="erp_submit_sales_order",
            arguments={"bill_no": "SO000004"},
        ),
    ]

    text = build_action_summary(calls, _risk("confirm", 35))

    # 工具名、参数名、风险档位全部转成中文；空参数不展示、整数浮点去尾
    assert "- 创建销售订单（客户：CUS001，数量：20）" in text
    assert "- 提交销售订单（单据编号：SO000004）" in text
    assert "备注" not in text
    assert "风险评分：35.0（单票确认）" in text


def test_build_action_summary_unknown_tool_falls_back() -> None:
    calls = [ToolCallDecision(tool_name="kingdee_update_sales_order", arguments={"amount": 5000})]

    text = build_action_summary(calls, _risk("confirm", 35))

    assert "kingdee_update_sales_order（amount：5000）" in text


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "是", "确认", "OK"])
async def test_cli_io_affirmative(answer: str) -> None:
    io = CliConfirmationIO(read_input=lambda prompt: answer)

    assert await io.ask("确认？") is True


@pytest.mark.parametrize("answer", ["n", "no", "", "随便"])
async def test_cli_io_negative(answer: str) -> None:
    io = CliConfirmationIO(read_input=lambda prompt: answer)

    assert await io.ask("确认？") is False


async def test_cli_io_eof_is_rejected() -> None:
    def _raise_eof(prompt: str) -> str:
        raise EOFError

    io = CliConfirmationIO(read_input=_raise_eof)

    assert await io.ask("确认？") is False


def test_results_are_json_serializable() -> None:
    decision = HitlDecision(level="confirm", reason="测试", approvers=("manager",))
    outcome = HitlOutcome(approved=False, cancelled=True, level="confirm", message="取消")

    assert json.loads(json.dumps(decision.to_dict()))["level"] == "confirm"
    assert json.loads(json.dumps(outcome.to_dict()))["cancelled"] is True
