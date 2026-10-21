"""Guard 5（M4-T06）：写操作业务必填项 + 引用字段必须内码 Id。

方案 §5.1 第五层：Guard 1 只校验 Schema 形状，业务规则需要单独声明——
例如 Schema 允许空串，但"创建订单必须有客户"是业务约束。

设计要点：
- **只处理写/动作工具**；查询工具跳过。
- **业务必填项**来自 ``ToolDefinition.required_parameters``，可多于 Schema 的
  ``required``；写工具未声明规则 = 规则缺失 = 拦截（fail-closed）。
- **引用字段必须是内码 Id**：``entity_references`` 声明的参数必须是正整数，
  名称/编码一律拒绝。Guard 3 已完成「名称 → FID」换码，管道会把
  ``parameter_updates`` 合并进后续 Guard 的上下文（T01 已支持）。
- 本 Guard 不查库、不调 LLM，纯参数校验。
"""

from collections.abc import Mapping
from typing import Any

from agent.tools import ToolRegistry
from infrastructure.exceptions import ToolError
from kingdee.endpoints.labels import param_label, tool_label
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_params"

# 需要业务参数校验的工具类型（写操作与动作类操作）
_WRITE_TOOL_TYPES = frozenset({"write", "action"})


class ParamGuard:
    """Guard 5：写操作业务必填项与引用字段内码校验。"""

    name = GUARD_NAME

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    async def check(self, context: GuardContext) -> GuardResult:
        """校验业务必填项与引用字段格式。"""
        try:
            tool = self._tools.get(context.tool_name)
        except ToolError:
            return GuardResult.block(
                f"工具「{tool_label(context.tool_name)}」未注册，无法校验写操作参数完整性",
                detail={"tool": context.tool_name},
            )

        if tool.tool_type not in _WRITE_TOOL_TYPES:
            return GuardResult.allow(
                detail={
                    "tool": context.tool_name,
                    "skipped": True,
                    "reason": "只读工具无需写参数校验",
                }
            )

        if not tool.required_parameters:
            return GuardResult.block(
                f"写操作工具「{tool_label(context.tool_name)}」未声明业务必填项规则（fail-closed）",
                detail={"tool": context.tool_name, "branch": "missing_rules"},
            )

        missing = [
            parameter
            for parameter in tool.required_parameters
            if _is_blank(context.arguments.get(parameter))
        ]
        if missing:
            return GuardResult.block(
                f"写操作缺少业务必填项：{'、'.join(param_label(parameter) for parameter in missing)}",
                detail={
                    "tool": context.tool_name,
                    "missing": missing,
                    "required": list(tool.required_parameters),
                },
            )

        invalid_ids = [
            {
                "parameter": reference.parameter,
                "entity_type": reference.entity_type,
                "value_type": type(context.arguments.get(reference.parameter)).__name__,
            }
            for reference in tool.entity_references
            if not _is_blank(context.arguments.get(reference.parameter))
            and not _is_internal_id(context.arguments.get(reference.parameter))
        ]
        if invalid_ids:
            invalid_detail = "、".join(
                f"{param_label(str(item['parameter']))}"
                f"「{context.arguments.get(item['parameter'])}」"
                for item in invalid_ids
            )
            return GuardResult.block(
                f"引用字段必须是内码 Id（正整数）：{invalid_detail}",
                detail={"tool": context.tool_name, "invalid_ids": invalid_ids},
            )

        checked_ids = [
            reference.parameter
            for reference in tool.entity_references
            if not _is_blank(context.arguments.get(reference.parameter))
        ]
        return GuardResult.allow(
            detail={
                "tool": context.tool_name,
                "required_checked": list(tool.required_parameters),
                "id_checked": checked_ids,
            }
        )


def _is_blank(value: Any) -> bool:
    """必填项空值判定：None / 空白串 / 空容器视为缺失。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping):
        return not value
    if isinstance(value, (list, tuple, set)):
        return not value
    return False


def _is_internal_id(value: Any) -> bool:
    """内码 Id 判定：正整数或纯数字字符串（金蝶 FID 为数值型）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        text = value.strip()
        return text.isdigit() and int(text) > 0
    return False
