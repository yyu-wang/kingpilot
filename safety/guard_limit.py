"""Guard 4（M4-T05）：分页条数硬上限 + 无条件查询拒绝。

方案 §5.1 第四层：``PageSize ≤ 10000``，且禁止无条件查询（防全表扫描）。

设计要点：
- **全局兜底**：当前查询工具的参数模型已把 ``limit`` 收紧到 1~100；
  本 Guard 的 10000 上限面向未来新工具，防止某个工具把分页放宽到失控。
- **只对查询工具检查过滤条件**：写/动作工具没有查询过滤语义，不套用本规则。
- **fail-closed**：参数无法校验、过滤条件组装失败，一律拦截。
- 无条件查询的职责从 Guard 2 移交到本 Guard（Guard 2 只校验「有条件的查询是否安全」）。
"""

from pydantic import ValidationError

from agent.tools import ToolRegistry
from infrastructure.exceptions import ToolError
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_limit"

# 分页硬上限（方案 §5.1）；不是默认页宽——默认值仍由各工具的参数模型决定
_MAX_PAGE_SIZE = 10_000


class LimitGuard:
    """Guard 4：分页条数硬限 + 查询必须带过滤条件。"""

    name = GUARD_NAME

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    async def check(self, context: GuardContext) -> GuardResult:
        """校验分页参数；查询工具额外要求至少一个过滤条件。"""
        try:
            tool = self._tools.get(context.tool_name)
        except ToolError:
            return GuardResult.block(
                f"工具 {context.tool_name} 未注册，无法校验分页与查询条件",
                detail={"tool": context.tool_name},
            )

        try:
            params = tool.params_model.model_validate(dict(context.arguments))
        except ValidationError as exc:
            # 无法确认分页/过滤参数 = 拒绝；Guard 1 通常会先拦住，这里是独立兜底
            return GuardResult.block(
                f"参数校验失败，无法校验分页与查询条件: {context.tool_name}",
                detail={"tool": context.tool_name, "error_count": len(exc.errors())},
            )

        if tool.tool_type != "query" or tool.is_local:
            # 写/动作工具无分页参数；本地工具（如 kb_search）没有 FilterString。
            # 必须先判类型再读 limit：写工具的参数模型本来就没有 limit。
            return GuardResult.allow(
                detail={
                    "tool": context.tool_name,
                    "tool_type": tool.tool_type,
                    "condition_present": False,
                    "local": tool.is_local,
                }
            )

        limit = getattr(params, "limit", None)
        if not isinstance(limit, int) or isinstance(limit, bool):
            return GuardResult.block(
                f"工具参数缺少整数 limit: {context.tool_name}",
                detail={"tool": context.tool_name, "branch": "missing_limit"},
            )
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            return GuardResult.block(
                f"分页参数 limit={limit} 超出允许范围 1~{_MAX_PAGE_SIZE}",
                detail={
                    "tool": context.tool_name,
                    "limit": limit,
                    "max_page_size": _MAX_PAGE_SIZE,
                },
            )

        if tool.build_filter is None:
            return GuardResult.block(
                f"工具缺少过滤条件组装器，无法校验查询条件: {context.tool_name}",
                detail={"tool": context.tool_name, "branch": "missing_builder"},
            )

        try:
            filter_string = tool.build_filter(params)
        except ValueError as exc:
            # 组装层拒绝（如过滤值含单引号）——fail-closed
            return GuardResult.block(
                f"过滤条件组装失败: {context.tool_name}（{type(exc).__name__}）",
                detail={"tool": context.tool_name, "error_type": type(exc).__name__},
            )

        if not filter_string.strip():
            return GuardResult.block(
                "禁止无条件查询（全表扫描）：请提供至少一个过滤条件",
                detail={
                    "tool": context.tool_name,
                    "limit": limit,
                    "filter_empty": True,
                },
            )

        return GuardResult.allow(
            detail={
                "tool": context.tool_name,
                "tool_type": tool.tool_type,
                "limit": limit,
                "condition_present": True,
            }
        )
