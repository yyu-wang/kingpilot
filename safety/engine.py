"""安全层编排（M4-T10）：把六层 Guard、权限、风险与 HITL 串成一次审查。

调用链：
    Phase 1 工具列表过滤（visible_tools）
    Phase 2 review：
        每条 tool_call → Guard 管道（权限兜底 + Guard 1~5）
        → effective_arguments（实体换码）
        → 权限判定（行过滤/列禁用）
        → 风险评分（多条取最高）
        → HITL 路由 + 一次确认覆盖整批
    Phase 3 由主循环按 GuardedCall 执行

设计原则：
- 任一调用被拦截 = 整批不执行（避免业务半成品）
- 安全层不调用 LLM；确认交互通过 ``ConfirmationIO`` 注入
- 本模块不依赖 ``agent.core``，由 core 反向依赖，避免循环导入
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent.tools import ToolRegistry
from config.permission_rules import DataDomain
from erp import FilterCondition, FilterExpression, FilterOp
from erp.protocol import ErpClient
from infrastructure.exceptions import ToolError
from kingdee.entity_lookup import KingdeeEntityLookup
from models.llm import ToolCallDecision, ToolSpec
from models.user import UserContext
from observability.audit import AuditLogger
from safety.approval_gateway import ApprovalGateway, ApprovalRequest
from safety.guard import GuardContext, GuardPipeline, GuardPipelineResult
from safety.guard_business import InboundPreflightGuard
from safety.guard_entity import EntityPreflightGuard
from safety.guard_limit import LimitGuard
from safety.guard_params import ParamGuard
from safety.guard_schema import SchemaGuard
from safety.guard_sql import FilterGuard
from safety.human_in_loop import (
    ConfirmationIO,
    HitlOutcome,
    HumanInTheLoop,
    build_action_summary,
)
from safety.permission_engine import (
    AccessType,
    PermissionDecision,
    PermissionEngine,
    PermissionGuard,
)
from safety.risk_engine import RiskDecision, RiskEngine, build_risk_context


@dataclass(frozen=True)
class GuardedCall:
    """安全审查通过的一次调用（含换码后参数与行/列限制）。"""

    tool_name: str
    arguments: Mapping[str, Any]
    # 行级过滤（结构化表达式，空 = 无行限制）；执行期与 LLM 侧条件按 AND 合并
    row_filter: FilterExpression = field(default_factory=FilterExpression)
    denied_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SafetyVerdict:
    """一次规划的安全审查结论。"""

    proceed: bool
    calls: tuple[GuardedCall, ...] = ()
    message: str = ""
    blocked: bool = False
    cancelled: bool = False
    read_only: bool = False
    approval_pending: bool = False
    approval_id: str = ""
    guard_records: tuple[dict[str, Any], ...] = ()
    risk: RiskDecision | None = None
    permission: PermissionDecision | None = None
    hitl: HitlOutcome | None = None


class SafetyLayer:
    """安全审查门面：工具过滤 + Guard 管道 + 风险/权限/HITL。"""

    def __init__(
        self,
        tools: ToolRegistry,
        client: ErpClient,
        hitl_io: ConfirmationIO,
        *,
        clock: Callable[[], datetime] | None = None,
        audit: AuditLogger | None = None,
        approval_gateway: ApprovalGateway | None = None,
    ) -> None:
        self._tools = tools
        self._permission = PermissionEngine(audit=audit)
        self._risk = RiskEngine()
        self._hitl = HumanInTheLoop(hitl_io)
        # 可注入时钟：测试固定工作时间，避免「非工作时间 +3 分」造成结果漂移
        self._clock = clock
        # None = CLI/无审批模式；API 模式注入网关，把 dual_approve 落成审批单
        self._approval_gateway = approval_gateway
        self._pipeline = GuardPipeline(
            [
                # 拦截点 2：执行前权限兜底（先失败快）
                PermissionGuard(tools, self._permission),
                SchemaGuard(tools),
                FilterGuard(tools),
                # 业务一致性预检在实体换码前执行：可直接比对用户输入的供应商名称/编码
                InboundPreflightGuard(client),
                EntityPreflightGuard(tools, KingdeeEntityLookup(client)),
                LimitGuard(tools),
                ParamGuard(tools),
            ],
            audit=audit,
        )

    def visible_tools(self, user: UserContext) -> list[ToolSpec]:
        """拦截点 1：按角色过滤 LLM 可见工具列表。"""
        return self._permission.domain_filter(user, self._tools.all_tools())

    def permission_denial(
        self, user: UserContext, tool_name: str, arguments: Mapping[str, Any] | None = None
    ) -> str | None:
        """权限预检：无权限时返回面向用户的拒绝文案，有权限返回 ``None``。

        Why 独立于 ``review``：无权限的工具不会下发给 LLM，模型不会主动调用，
        只能由主循环在「空规划」时反查目标工具并直接给出权限提示，避免
        用户看到「没有查询到数据」这种误导性答复。
        """
        try:
            tool = self._tools.get(tool_name)
        except ToolError:
            return None
        decision = self._permission.check(user, tool, dict(arguments or {}))
        if decision.allowed:
            return None
        return f"权限不足：{decision.reason}"

    async def review(
        self,
        user: UserContext,
        calls: Sequence[ToolCallDecision],
        trace_id: str,
        *,
        session_id: str = "",
    ) -> SafetyVerdict:
        """逐条审查规划；任一条拦截则整批不执行。"""
        records: list[dict[str, Any]] = []
        guarded: list[GuardedCall] = []
        permissions: list[PermissionDecision] = []
        risks: list[RiskDecision] = []
        now = self._clock() if self._clock is not None else None

        for call in calls:
            context = GuardContext(
                tool_name=call.tool_name,
                arguments=call.arguments,
                user_context=user,
                trace_id=trace_id,
            )
            result = await self._pipeline.check(context)
            records.extend(result.to_records())
            if result.blocked:
                return SafetyVerdict(
                    proceed=False,
                    message=_block_message(result),
                    blocked=True,
                    guard_records=tuple(records),
                    permission=_permission_from_record(result),
                )

            tool = self._tools.get(call.tool_name)
            arguments = dict(result.effective_arguments)
            decision = self._permission.check(user, tool, arguments, trace_id=trace_id)
            if not decision.allowed:
                return SafetyVerdict(
                    proceed=False,
                    message=decision.reason,
                    blocked=True,
                    guard_records=tuple(records),
                    permission=decision,
                )

            permissions.append(decision)
            risks.append(self._risk.assess(build_risk_context(tool, arguments, user, now=now)))
            guarded.append(
                GuardedCall(
                    tool_name=call.tool_name,
                    arguments=arguments,
                    row_filter=decision.row_filter,
                    denied_fields=decision.denied_fields,
                )
            )

        if not guarded:
            return SafetyVerdict(proceed=True, guard_records=tuple(records))

        risk = max(risks, key=lambda item: item.score)
        permission = _merge_permissions(permissions)
        hitl_decision = self._hitl.route(risk, permission)
        # 审批单保存换码后的参数：执行时不能再依赖 LLM 原始输入
        effective_calls = tuple(
            ToolCallDecision(
                tool_name=guarded_call.tool_name,
                arguments=dict(guarded_call.arguments),
                rationale=source.rationale,
            )
            for source, guarded_call in zip(calls, guarded, strict=True)
        )
        summary = build_action_summary(effective_calls, risk)
        # 审批单一次只承载一个写调用（网关约束）；查询类调用继续走 HITL
        single_write = len(guarded) == 1 and self._tools.get(guarded[0].tool_name).is_write
        if (
            self._approval_gateway is not None
            and single_write
            and hitl_decision.level in ("confirm", "dual_approve")
        ):
            # confirm = 1 票；dual_approve = 2 票（不同审批人）
            required_approvals = 1 if hitl_decision.level == "confirm" else 2
            approval_id = await self._approval_gateway.request(
                ApprovalRequest(
                    trace_id=trace_id,
                    session_id=session_id,
                    user=user,
                    calls=effective_calls,
                    risk=risk,
                    permission=permission,
                    summary=summary,
                    required_approvals=required_approvals,
                )
            )
            mode = "单票审批" if required_approvals == 1 else "双人复核"
            return SafetyVerdict(
                proceed=False,
                approval_pending=True,
                approval_id=approval_id,
                message=f"已提交{mode}（{approval_id}），审批通过后自动执行。",
                guard_records=tuple(records),
                risk=risk,
                permission=permission,
            )
        outcome = await self._hitl.confirm(hitl_decision, summary)

        if not outcome.approved:
            return SafetyVerdict(
                proceed=False,
                message=outcome.message,
                blocked=outcome.blocked,
                cancelled=outcome.cancelled,
                read_only=outcome.read_only,
                guard_records=tuple(records),
                risk=risk,
                permission=permission,
                hitl=outcome,
            )

        return SafetyVerdict(
            proceed=True,
            calls=tuple(guarded),
            guard_records=tuple(records),
            risk=risk,
            permission=permission,
            hitl=outcome,
        )


def _block_message(result: GuardPipelineResult) -> str:
    """拦截文案：实体预检（部分/全部未命中）时优先用面向用户的具体提示。"""
    if result.records:
        detail = result.records[-1].detail
        user_message = detail.get("user_message")
        if isinstance(user_message, str) and user_message:
            return user_message
    return result.reason


def _permission_from_record(
    result: GuardPipelineResult,
) -> PermissionDecision | None:
    """从 Guard 记录还原权限判定（拦截时也把权限结果写进 trace）。"""
    for record in result.records:
        if record.guard_name != "guard_permission":
            continue
        detail = record.detail
        domain_value = detail.get("domain")
        domain = (
            DataDomain(domain_value) if isinstance(domain_value, str) and domain_value else None
        )
        access: AccessType = "write" if detail.get("access_type") == "write" else "read"
        return PermissionDecision(
            allowed=bool(detail.get("allowed", False)),
            reason=str(detail.get("reason", "")),
            domain=domain,
            access_type=access,
            row_filter=_as_filter_expression(detail.get("row_filter")),
            denied_fields=_as_str_tuple(detail.get("denied_fields")),
            required_approvers=_as_str_tuple(detail.get("required_approvers")),
            rule_id=str(detail.get("rule_id", "")),
        )
    return None


def _as_filter_expression(value: Any) -> FilterExpression:
    """把审计 detail 里的条件列表还原为 FilterExpression；非法形状按空处理。"""
    if not isinstance(value, (list, tuple)):
        return FilterExpression()
    conditions: list[FilterCondition] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        field_name = item.get("field")
        op_value = item.get("op")
        condition_value = item.get("value")
        if not isinstance(field_name, str) or not isinstance(op_value, str):
            continue
        if not isinstance(condition_value, (str, int, float)) or isinstance(condition_value, bool):
            continue
        try:
            op = FilterOp(op_value)
        except ValueError:
            continue
        value_arg = (
            float(condition_value) if isinstance(condition_value, (int, float)) else condition_value
        )
        conditions.append(FilterCondition(field=field_name, op=op, value=value_arg))
    return FilterExpression(conditions=tuple(conditions))


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _merge_permissions(
    decisions: Sequence[PermissionDecision],
) -> PermissionDecision:
    """把逐条权限判定合并成一份审计/路由用的聚合结果。"""
    if not decisions:
        return PermissionDecision(allowed=True)

    approvers = tuple(
        dict.fromkeys(
            approver for decision in decisions for approver in decision.required_approvers
        )
    )
    reasons = [decision.reason for decision in decisions if decision.reason]
    denied_fields = tuple(
        sorted({field for decision in decisions for field in decision.denied_fields})
    )
    row_filter_conditions = tuple(
        condition for decision in decisions for condition in decision.row_filter.conditions
    )
    return PermissionDecision(
        allowed=all(decision.allowed for decision in decisions),
        reason="；".join(reasons),
        domain=decisions[0].domain,
        access_type=decisions[0].access_type,
        row_filter=FilterExpression(conditions=row_filter_conditions),
        denied_fields=denied_fields,
        required_approvers=approvers,
        rule_id=decisions[0].rule_id,
    )
