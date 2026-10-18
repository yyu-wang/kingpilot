"""四层权限与数据域隔离（M4-T08，方案 §5.1 支柱五及扩展）。

四层判定：
1. **工具层**：``UserContext.allow_tools / deny_tools``（deny 优先）
2. **数据域**：表单 → 域 → 角色可见/可写矩阵（可见 ≠ 可写）
3. **行级**：``data_scope.org_ids / owner_only`` → 强制行过滤表达式
4. **列级**：``COLUMN_DENY_MAP`` → 结果出口需剔除的字段

另含写规则（金额升级/禁止删除）与两个拦截点：
- 拦截点 1：``domain_filter()`` 按角色过滤 LLM 可见工具列表
- 拦截点 2：``PermissionGuard`` 作为执行前服务端兜底（fail-closed）

身份只信 ``UserContext``（来自登录态/session），不读任何 LLM 自述；
未知角色、未知表单、无法构造行过滤均拒绝。行过滤的实际注入执行归 T10。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from agent.tools import ToolDefinition, ToolRegistry
from config.permission_rules import (
    COLUMN_DENY_MAP,
    FORM_DOMAIN_MAP,
    ROLE_DOMAIN_MATRIX,
    ROW_OWNER_FIELD_MAP,
    ROW_SCOPE_FIELD_MAP,
    WRITE_RULES,
    DataDomain,
    DomainAccess,
    WriteRule,
)
from infrastructure.exceptions import ToolError
from kingdee.endpoints.base import quote_eq
from models.audit import AuditEvent
from models.llm import ToolSpec
from models.user import UserContext
from observability.audit import AuditLogger
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_permission"

AccessType = Literal["read", "write"]

_DOMAIN_LABELS: dict[DataDomain, str] = {
    DataDomain.SALES: "销售",
    DataDomain.PURCHASE: "采购",
    DataDomain.INVENTORY: "库存",
    DataDomain.FINANCE: "财务",
    DataDomain.HR: "人事",
    DataDomain.PRODUCTION: "生产",
}


@dataclass(frozen=True)
class PermissionDecision:
    """一次工具调用的权限判定结果（写进 trace.permission_check）。"""

    allowed: bool
    reason: str = ""
    domain: DataDomain | None = None
    access_type: AccessType = "read"
    row_filter: str = ""
    denied_fields: tuple[str, ...] = ()
    required_approvers: tuple[str, ...] = ()
    rule_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "domain": self.domain.value if self.domain is not None else "",
            "access_type": self.access_type,
            "row_filter": self.row_filter,
            "denied_fields": list(self.denied_fields),
            "required_approvers": list(self.required_approvers),
            "rule_id": self.rule_id,
        }


class PermissionEngine:
    """规则表驱动的四层权限判定。"""

    def __init__(
        self,
        *,
        matrix: Mapping[str, DomainAccess] = ROLE_DOMAIN_MATRIX,
        form_domains: Mapping[str, DataDomain] = FORM_DOMAIN_MAP,
        write_rules: Sequence[WriteRule] = WRITE_RULES,
        row_scope_fields: Mapping[str, str] = ROW_SCOPE_FIELD_MAP,
        row_owner_fields: Mapping[str, str] = ROW_OWNER_FIELD_MAP,
        column_deny: Mapping[DataDomain, frozenset[str]] = COLUMN_DENY_MAP,
        audit: AuditLogger | None = None,
    ) -> None:
        """装配引擎；空矩阵/空域映射属于配置缺失，构造期拒绝（fail-closed）。"""
        if not matrix:
            raise ValueError("角色权限矩阵不能为空（fail-closed）")
        if not form_domains:
            raise ValueError("表单域映射不能为空（fail-closed）")
        self._matrix = dict(matrix)
        self._form_domains = dict(form_domains)
        self._write_rules = tuple(write_rules)
        self._row_scope_fields = dict(row_scope_fields)
        self._row_owner_fields = dict(row_owner_fields)
        self._column_deny = dict(column_deny)
        self._audit = audit

    # ── 拦截点 1：工具列表过滤 ──

    def domain_filter(self, user: UserContext, tools: Sequence[ToolDefinition]) -> list[ToolSpec]:
        """按「工具层 + 数据域」过滤 LLM 可见工具（无权限的工具根本不下发）。"""
        specs: list[ToolSpec] = []
        for tool in tools:
            access: AccessType = "read" if tool.tool_type == "query" else "write"
            if tool.name in user.deny_tools:
                continue
            if user.allow_tools and tool.name not in user.allow_tools:
                continue
            role_access = self._matrix.get(user.role)
            if role_access is None:
                continue
            domain = self._form_domains.get(tool.endpoint.form_id)
            if domain is None:
                continue
            allowed_domains = role_access.visible if access == "read" else role_access.writable
            if domain in allowed_domains:
                specs.append(tool.spec)
        return specs

    # ── 拦截点 2：执行前完整判定 ──

    def check(
        self,
        user: UserContext,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        *,
        trace_id: str = "",
    ) -> PermissionDecision:
        """按工具层 → 数据域 → 行级 → 写规则 → 列级顺序判定。"""
        access: AccessType = "read" if tool.tool_type == "query" else "write"
        form_id = tool.endpoint.form_id

        # 1) 工具层
        if tool.name in user.deny_tools:
            return self._deny(user, tool, None, access, "该工具被显式禁用", trace_id=trace_id)
        if user.allow_tools and tool.name not in user.allow_tools:
            return self._deny(user, tool, None, access, "该工具不在允许列表内", trace_id=trace_id)

        # 2) 角色 + 数据域
        role_access = self._matrix.get(user.role)
        if role_access is None:
            return self._deny(user, tool, None, access, f"未知角色: {user.role}", trace_id=trace_id)

        domain = self._form_domains.get(form_id)
        if domain is None:
            return self._deny(
                user,
                tool,
                None,
                access,
                f"未知表单 {form_id}，无法确定数据域",
                trace_id=trace_id,
            )

        allowed_domains = role_access.visible if access == "read" else role_access.writable
        if domain not in allowed_domains:
            reason = (
                f"您当前角色暂无{_domain_label(domain)}数据的访问权限。"
                "如需查看，请联系管理员开通对应数据域权限。"
            )
            return self._deny(user, tool, domain, access, reason, trace_id=trace_id)

        # 数据范围（表单白名单）
        scope_form_ids = _str_list(user.data_scope.get("form_ids"))
        if scope_form_ids and form_id not in scope_form_ids:
            return self._deny(
                user,
                tool,
                domain,
                access,
                f"单据 {form_id} 不在您的数据范围内",
                trace_id=trace_id,
            )

        # 3) 行级
        row_filter, row_denial = self._build_row_filter(user, form_id)
        if row_denial is not None:
            return self._deny(user, tool, domain, access, row_denial, trace_id=trace_id)

        # 4) 写规则（仅写/动作）
        approvers: tuple[str, ...] = ()
        rule_id = ""
        reason = ""
        if access == "write":
            denial_rule, approvers, rule_id = self._match_write_rules(user, tool, arguments)
            if denial_rule is not None:
                return self._deny(
                    user,
                    tool,
                    domain,
                    access,
                    f"写操作被权限规则拒绝: {denial_rule.label}",
                    rule_id=denial_rule.rule_id,
                    trace_id=trace_id,
                )
            if approvers:
                reason = f"写操作需升级审批: {'、'.join(approvers)}"

        # 5) 列级
        denied_fields = tuple(
            sorted(
                set(self._column_deny.get(domain, frozenset()))
                | set(_str_list(user.data_scope.get("denied_fields")))
            )
        )
        return PermissionDecision(
            allowed=True,
            reason=reason,
            domain=domain,
            access_type=access,
            row_filter=row_filter,
            denied_fields=denied_fields,
            required_approvers=approvers,
            rule_id=rule_id,
        )

    def _build_row_filter(self, user: UserContext, form_id: str) -> tuple[str, str | None]:
        """构造行级过滤；失败返回 (空串, 拒绝原因)。"""
        parts: list[str] = []

        org_ids = _str_list(user.data_scope.get("org_ids"))
        if org_ids:
            if len(org_ids) > 1:
                return "", "多组织行级过滤暂未支持，请指定单一组织"
            field = self._row_scope_fields.get(form_id)
            if field is None:
                return "", f"{form_id} 未配置组织级行过滤字段"
            quoted = _quote_value(org_ids[0])
            if quoted is None:
                return "", "组织标识包含非法字符"
            parts.append(f"{field} = {quoted}")

        if bool(user.data_scope.get("owner_only")):
            owner_field = self._row_owner_fields.get(form_id)
            if owner_field is None:
                return "", f"{form_id} 未配置负责人行过滤字段"
            quoted = _quote_value(user.user_id)
            if quoted is None:
                return "", "用户标识包含非法字符"
            parts.append(f"{owner_field} = {quoted}")

        return " AND ".join(parts), None

    def _match_write_rules(
        self,
        user: UserContext,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> tuple[WriteRule | None, tuple[str, ...], str]:
        """返回 (拒绝规则, 审批角色列表, 命中规则 ID)；deny 优先于 escalate。"""
        amount = _as_float(arguments.get("amount"))
        matched: list[WriteRule] = []
        for rule in self._write_rules:
            if rule.roles and user.role not in rule.roles:
                continue
            if rule.form_ids and tool.endpoint.form_id not in rule.form_ids:
                continue
            if rule.actions and tool.action not in rule.actions:
                continue
            if rule.max_amount is not None and amount <= rule.max_amount:
                continue
            matched.append(rule)

        denials = [rule for rule in matched if rule.decision == "deny"]
        if denials:
            return denials[0], (), denials[0].rule_id

        escalations = [rule for rule in matched if rule.decision == "escalate" and rule.escalate_to]
        approvers = tuple(rule.escalate_to for rule in escalations)
        rule_id = escalations[0].rule_id if escalations else ""
        return None, approvers, rule_id

    def _deny(
        self,
        user: UserContext,
        tool: ToolDefinition,
        domain: DataDomain | None,
        access: AccessType,
        reason: str,
        *,
        rule_id: str = "",
        trace_id: str = "",
    ) -> PermissionDecision:
        """构造拒绝结果并落审计事件（未注入审计时退回结构化日志）。"""
        if self._audit is not None:
            self._audit.emit(
                AuditEvent(
                    event_type="permission.denied",
                    trace_id=trace_id,
                    user_id=user.user_id,
                    user_role=user.role,
                    ip_address=user.ip_address,
                    action=tool.action,
                    form_id=tool.endpoint.form_id,
                    tool_name=tool.name,
                    status="rejected",
                    detail={
                        "domain": domain.value if domain is not None else "",
                        "access_type": access,
                        "reason": reason,
                        "rule_id": rule_id,
                    },
                )
            )
        else:
            logger.warning(
                "越权尝试 | event=security.denied tool={} form={} domain={} access={} "
                "user={} role={} reason={}",
                tool.name,
                tool.endpoint.form_id,
                domain.value if domain is not None else "-",
                access,
                user.user_id,
                user.role,
                reason,
            )
        return PermissionDecision(
            allowed=False,
            reason=reason,
            domain=domain,
            access_type=access,
            rule_id=rule_id,
        )


class PermissionGuard:
    """Guard 协议实现：执行前按四层权限兜底（拦截点 2）。"""

    name = GUARD_NAME

    def __init__(self, tools: ToolRegistry, engine: PermissionEngine) -> None:
        self._tools = tools
        self._engine = engine

    async def check(self, context: GuardContext) -> GuardResult:
        """解析工具 → 权限判定 → 拒绝则中断管道。"""
        try:
            tool = self._tools.get(context.tool_name)
        except ToolError:
            return GuardResult.block(
                f"工具 {context.tool_name} 未注册，无法校验权限",
                detail={"tool": context.tool_name},
            )

        decision = self._engine.check(
            context.user_context,
            tool,
            context.arguments,
            trace_id=context.trace_id,
        )
        if not decision.allowed:
            return GuardResult.block(decision.reason, detail=decision.to_dict())
        return GuardResult.allow(detail=decision.to_dict())


def apply_column_filter(
    rows: Sequence[Mapping[str, Any]], denied_fields: Sequence[str]
) -> list[dict[str, Any]]:
    """列级权限落地：从结果行中剔除禁用字段（T10 结果出口调用）。"""
    if not denied_fields:
        return [dict(row) for row in rows]
    denied = set(denied_fields)
    return [{key: value for key, value in row.items() if key not in denied} for row in rows]


def _domain_label(domain: DataDomain) -> str:
    return _DOMAIN_LABELS.get(domain, domain.value)


def _str_list(value: Any) -> list[str]:
    """data_scope 中的字符串列表；非法形状返回空（调用方按“未设置”处理）。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _quote_value(value: str) -> str | None:
    """行过滤字面量；单引号等非法字符返回 None（fail-closed）。"""
    try:
        return quote_eq(value)
    except ValueError:
        return None


def _as_float(value: Any) -> float:
    """金额提取：数值或数字字符串；其它类型按 0。"""
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
