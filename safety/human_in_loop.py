"""HITL 四档分级路由与确认等待（M4-T09，方案 §5.1 支柱一）。

路由**只依赖**风险决策与权限决策（禁止旁路）：
    auto         → 直接执行
    confirm      → 展示摘要，一次确认
    dual_approve → 两次确认（第一确认人 + 复核人，CLI 模拟双人复核）
    read_only    → 不执行，只展示建议，提示人工处理

权限升级（``required_approvers``）会把档位提升到至少 dual_approve；
权限拒绝则直接 blocked。本模块不接主循环（T10 统一接线）。
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from config.risk_rules import ApprovalLevel
from models.llm import ToolCallDecision
from safety.permission_engine import PermissionDecision
from safety.risk_engine import RiskDecision

_LEVEL_ORDER: dict[ApprovalLevel, int] = {
    "auto": 0,
    "confirm": 1,
    "dual_approve": 2,
    "read_only": 3,
}

_AFFIRMATIVE = frozenset({"y", "yes", "是", "确认", "ok"})

_READ_ONLY_MESSAGE = "该操作风险较高，系统仅提供建议，请人工执行。"


@dataclass(frozen=True)
class HitlDecision:
    """路由结果（写进 trace.risk_assessment / permission_check 的关联记录）。"""

    level: ApprovalLevel
    reason: str = ""
    approvers: tuple[str, ...] = ()
    blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "level": self.level,
            "reason": self.reason,
            "approvers": list(self.approvers),
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class HitlOutcome:
    """确认等待的结果；T10 据此决定执行或取消。"""

    approved: bool
    cancelled: bool = False
    read_only: bool = False
    blocked: bool = False
    level: ApprovalLevel = "auto"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "approved": self.approved,
            "cancelled": self.cancelled,
            "read_only": self.read_only,
            "blocked": self.blocked,
            "level": self.level,
            "message": self.message,
        }


class ConfirmationIO(Protocol):
    """确认交互接口（CLI/IM/审批系统均可实现）。"""

    async def ask(self, prompt: str) -> bool:
        """展示提示并返回用户是否同意。"""
        ...


class CliConfirmationIO:
    """CLI 确认实现：y/yes/是/确认/ok 视为同意，EOF 视为拒绝。"""

    def __init__(self, read_input: Callable[[str], str] = input) -> None:
        self._read_input = read_input

    async def ask(self, prompt: str) -> bool:
        """读取一行输入；``asyncio.to_thread`` 避免阻塞事件循环。"""
        try:
            answer = await asyncio.to_thread(self._read_input, prompt)
        except EOFError:
            return False
        return answer.strip().lower() in _AFFIRMATIVE


class HumanInTheLoop:
    """四档路由 + 确认等待。"""

    def __init__(self, io: ConfirmationIO) -> None:
        self._io = io

    def route(
        self,
        risk: RiskDecision,
        permission: PermissionDecision | None = None,
    ) -> HitlDecision:
        """按「更严格者优先」合并风险档位与权限升级。"""
        if permission is not None and not permission.allowed:
            return HitlDecision(
                level="read_only",
                reason=permission.reason,
                blocked=True,
            )

        level = risk.level
        approvers = list(risk.required_approvers)

        if permission is not None and permission.required_approvers:
            if _LEVEL_ORDER[level] < _LEVEL_ORDER["dual_approve"]:
                level = "dual_approve"
            approvers.extend(permission.required_approvers)

        unique_approvers = tuple(dict.fromkeys(approvers))
        reason = f"风险评分 {risk.score:.1f}，路由档位 {level}"
        if permission is not None and permission.reason:
            reason = f"{reason}；{permission.reason}"

        return HitlDecision(level=level, reason=reason, approvers=unique_approvers)

    async def confirm(self, decision: HitlDecision, summary: str = "") -> HitlOutcome:
        """按档位执行确认等待；auto/read_only/blocked 不打断用户。"""
        if decision.blocked:
            return HitlOutcome(
                approved=False,
                blocked=True,
                level="read_only",
                message=decision.reason,
            )
        if decision.level == "auto":
            return HitlOutcome(approved=True, level="auto")
        if decision.level == "read_only":
            return HitlOutcome(
                approved=False,
                read_only=True,
                level="read_only",
                message=_READ_ONLY_MESSAGE,
            )

        prompt = _build_prompt(decision, summary)
        if decision.level == "confirm":
            accepted = await self._io.ask(prompt)
            if accepted:
                return HitlOutcome(approved=True, level="confirm", message="已确认执行。")
            return HitlOutcome(
                approved=False,
                cancelled=True,
                level="confirm",
                message="已取消本次操作。",
            )

        # dual_approve：第一确认人 + 复核人
        first = await self._io.ask(f"【第一确认人】{prompt}")
        if not first:
            return HitlOutcome(
                approved=False,
                cancelled=True,
                level="dual_approve",
                message="第一确认人取消，操作未执行。",
            )
        reviewer = f"（{decision.approvers[0]}）" if decision.approvers else ""
        second = await self._io.ask(f"【复核人{reviewer}】{prompt}")
        if not second:
            return HitlOutcome(
                approved=False,
                cancelled=True,
                level="dual_approve",
                message="复核人取消，操作未执行。",
            )
        return HitlOutcome(
            approved=True,
            level="dual_approve",
            message="双人复核通过，已确认执行。",
        )


def build_action_summary(calls: Sequence[ToolCallDecision], risk: RiskDecision) -> str:
    """生成面向用户的确认摘要（工具、参数、风险分）。"""
    lines = ["即将执行以下操作："]
    for call in calls:
        if call.arguments:
            args = "，".join(f"{key}={value}" for key, value in call.arguments.items())
            lines.append(f"- {call.tool_name}（{args}）")
        else:
            lines.append(f"- {call.tool_name}")
    lines.append(f"风险评分：{risk.score:.1f}（档位 {risk.level}）")
    return "\n".join(lines)


def _build_prompt(decision: HitlDecision, summary: str) -> str:
    parts: list[str] = []
    if summary:
        parts.append(summary)
    parts.append(f"风险档位：{decision.level}；{decision.reason}")
    if decision.approvers:
        parts.append(f"需审批角色：{'、'.join(decision.approvers)}")
    parts.append("确认执行？(y/N) ")
    return "\n".join(parts)
