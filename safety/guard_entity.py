"""Guard 3（M4-T04）：写操作引用实体存在性预检 + 三分支处理。

方案 §5.1「实体存在性预检（防幻觉核心）」：
1. 全部存在 → 放行，并把名称换成系统规范编码（``parameter_updates``）
2. 部分幻觉 → 拦截，产出纠错 Prompt（T10 回喂 LLM 修正 Tool Call）
3. 全部幻觉 → 拦截，产出面向用户的提示（含相近候选）

设计要点：
- **只处理写/动作工具**：查询工具直接跳过（trace 与方案决策树的 SKIPPED 对齐）。
- **写工具必须声明实体引用**：未声明 = 规则缺失 = 拦截（fail-closed）。
- **纯函数**：本 Guard 不调 LLM、不改上下文；名称→编码的替换以
  ``detail["parameter_updates"]`` 交给主循环应用。
- **异常即拒绝**：解析器抛 ``KingPilotError`` 时直接拦截，不带着不确定状态执行。
"""

from dataclasses import dataclass
from typing import Any

from agent.tools import ToolRegistry
from infrastructure.exceptions import KingPilotError, ToolError
from kingdee.entity_lookup import EntityLookup
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_entity"

# 需要实体预检的工具类型（写操作与动作类操作）
_WRITE_TOOL_TYPES = frozenset({"write", "action"})


@dataclass(frozen=True)
class _Unresolved:
    """未解析成功的引用（审计与提示词生成的中间结构）。"""

    parameter: str
    identifier: str
    candidates: tuple[str, ...] = ()


class EntityPreflightGuard:
    """Guard 3：写操作引用实体先查后放行。"""

    name = GUARD_NAME

    def __init__(self, tools: ToolRegistry, lookup: EntityLookup) -> None:
        self._tools = tools
        self._lookup = lookup

    async def check(self, context: GuardContext) -> GuardResult:
        """按声明逐项解析实体引用，并按三分支给出结论。"""
        try:
            tool = self._tools.get(context.tool_name)
        except ToolError:
            return GuardResult.block(
                f"工具 {context.tool_name} 未注册，无法做实体预检",
                detail={"tool": context.tool_name},
            )

        if tool.tool_type not in _WRITE_TOOL_TYPES:
            return GuardResult.allow(
                detail={
                    "tool": context.tool_name,
                    "skipped": True,
                    "reason": "只读工具无需实体预检",
                }
            )

        if not tool.entity_references:
            return GuardResult.block(
                f"写操作工具 {context.tool_name} 未声明实体引用规则（fail-closed）",
                detail={"tool": context.tool_name, "branch": "missing_rules"},
            )

        resolved: dict[str, str] = {}
        unresolved: list[_Unresolved] = []
        for reference in tool.entity_references:
            identifier = _as_identifier(context.arguments.get(reference.parameter))
            if not identifier:
                # 空值不在这里判必填：Guard 5 负责业务必填项
                continue
            try:
                result = await self._lookup.lookup(
                    reference.entity_type,
                    identifier,
                    allow_name=reference.allow_name,
                )
            except KingPilotError as exc:
                return GuardResult.block(
                    "实体预检失败，按拒绝处理: "
                    f"{reference.entity_type}={identifier!r}（{type(exc).__name__}）",
                    detail={
                        "tool": context.tool_name,
                        "branch": "lookup_error",
                        "parameter": reference.parameter,
                        "entity_type": reference.entity_type,
                        "error_type": type(exc).__name__,
                    },
                )

            if result.found and result.canonical_id:
                resolved[reference.parameter] = result.canonical_id
            else:
                unresolved.append(
                    _Unresolved(
                        parameter=reference.parameter,
                        identifier=identifier,
                        candidates=result.candidates,
                    )
                )

        if not unresolved:
            return GuardResult.allow(
                detail={
                    "tool": context.tool_name,
                    "branch": "all_valid",
                    "parameter_updates": resolved,
                }
            )

        hallucinated = [_audit_unresolved(item) for item in unresolved]
        if resolved:
            return GuardResult.block(
                "实体预检部分失败：存在无法确认的引用实体",
                detail={
                    "tool": context.tool_name,
                    "branch": "partial",
                    "hallucinated": hallucinated,
                    "resolved": resolved,
                    "correction_prompt": _build_correction_prompt(unresolved, resolved),
                },
            )

        return GuardResult.block(
            "实体预检失败：引用的实体均不存在",
            detail={
                "tool": context.tool_name,
                "branch": "all_missing",
                "hallucinated": hallucinated,
                "user_message": _build_user_message(unresolved),
            },
        )


def _as_identifier(value: Any) -> str:
    """参数值 → 待解析标识；非标量（对象/数组）留空交给 Guard 5 处理。"""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _audit_unresolved(item: _Unresolved) -> dict[str, Any]:
    """未解析项 → 审计友好字典（JSON 可序列化）。"""
    return {
        "parameter": item.parameter,
        "identifier": item.identifier,
        "candidates": list(item.candidates),
    }


def _build_correction_prompt(unresolved: list[_Unresolved], resolved: dict[str, str]) -> str:
    """部分幻觉分支的纠错 Prompt（T10 回喂 LLM，不由 Guard 发起调用）。"""
    lines = ["实体预检未通过，请修正以下参数后重新发起工具调用："]
    for item in unresolved:
        candidates = f"（相近：{'、'.join(item.candidates)}）" if item.candidates else ""
        lines.append(f"- {item.parameter} = {item.identifier!r}：系统中不存在{candidates}")
    if resolved:
        verified = "；".join(f"{param} = {value}" for param, value in resolved.items())
        lines.append(f"已核实：{verified}")
    lines.append("只能使用系统中存在的实体，不得编造；无法确认时请向用户追问。")
    return "\n".join(lines)


def _build_user_message(unresolved: list[_Unresolved]) -> str:
    """全部幻觉分支的面向用户提示（含相近候选）。"""
    parts: list[str] = []
    for item in unresolved:
        candidates = f"（相近的有：{'、'.join(item.candidates)}）" if item.candidates else ""
        parts.append(f"未找到实体「{item.identifier}」{candidates}")
    return "；".join(parts) + "。请确认名称或提供编码后重试。"
