"""Guard 2（M4-T03，T05 重写）：FilterExpression 字段白名单 + 值类型校验。

方案 §5.1 五层 Guard 的第二层。校验对象是**结构化的 FilterExpression**（不是参数，
也不是厂商方言字符串）：工具层先用参数模型组装 FilterExpression，本 Guard 再对
产物做纵深校验——即使未来某个 ``build_filter`` 写错或被污染，也能在发给厂商
适配器之前拦住。

T05 变更（重写）：
- 校验对象从 FilterString 字符串改为 ``FilterExpression``——字符串解析
  （引号掩码 / 受限文法）整体消失；危险模式黑名单设施下沉为金蝶渲染器的
  内部自检（``kingdee/filter_string.py``），本 Guard 不再接触任何查询方言。
- 新增**值类型匹配**校验：``FilterCondition.value`` 的类型必须与端点字段的
  ``data_type`` 一致（number 收数值、date 收 ``YYYY-MM-DD`` 字符串、其余收
  字符串），LIKE 不允许用于数值字段。

实现决策（保留）：
- **字段白名单**取端点的 ``filterable_fields``（可查 ≠ 可过滤，单一真相源在
  ``erp/descriptors.py``）。
- **空 FilterExpression 放行**：无条件查询拒绝归 Guard 4，避免同一规则两处实现。
- 参数非法 / 组装层拒绝 → fail-closed 拦截；其它异常由 Guard 管道统一兜底。
"""

import re

from pydantic import ValidationError

from agent.tools import ToolRegistry
from erp.descriptors import FieldType
from erp.types import FilterCondition, FilterOp
from infrastructure.exceptions import ToolError
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_filter"

__all__ = ["GUARD_NAME", "FilterGuard"]

# date 字段的过滤值形状（渲染期才决定引号， Guard 只校验形状）
_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FilterGuard:
    """Guard 2：对工具组装出的 FilterExpression 做字段白名单与值类型校验。"""

    name = GUARD_NAME

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    async def check(self, context: GuardContext) -> GuardResult:
        """按「组装 → 字段白名单 → 值类型匹配」顺序校验。"""
        try:
            tool = self._tools.get(context.tool_name)
        except ToolError:
            return GuardResult.block(
                f"工具 {context.tool_name} 未注册，无法校验过滤条件",
                detail={"tool": context.tool_name},
            )

        try:
            params = tool.params_model.model_validate(dict(context.arguments))
        except ValidationError as exc:
            return GuardResult.block(
                f"参数校验失败，无法校验过滤条件: {context.tool_name}",
                detail={"tool": context.tool_name, "error_count": len(exc.errors())},
            )

        if tool.tool_type != "query":
            # 写/动作工具不产生过滤表达式，没有注入面（参数已由 Guard 1/5 校验）
            return GuardResult.allow(
                detail={
                    "tool": context.tool_name,
                    "tool_type": tool.tool_type,
                    "skipped": True,
                }
            )

        if tool.is_local:
            # 本地工具不产生过滤表达式（无注入面），Schema 已由 Guard 1 校验
            return GuardResult.allow(
                detail={"tool": context.tool_name, "local": True, "skipped": True}
            )

        if tool.build_filter is None:
            return GuardResult.block(
                f"工具缺少过滤条件组装器: {context.tool_name}",
                detail={"tool": context.tool_name, "branch": "missing_builder"},
            )

        try:
            filter_expr = tool.build_filter(params)
        except ValueError as exc:
            # 组装层拒绝（如过滤值含单引号）——归一为拦截，不把原文回显进原因
            return GuardResult.block(
                f"过滤条件组装失败: {context.tool_name}（{type(exc).__name__}）",
                detail={"tool": context.tool_name, "error_type": type(exc).__name__},
            )

        if not filter_expr.conditions:
            # 无条件查询的拒绝归 Guard 4：本 Guard 只负责「有条件的查询是否安全」
            return GuardResult.allow(detail={"tool": context.tool_name, "filter_empty": True})

        if tool.endpoint is None:
            return GuardResult.block(
                f"工具缺少端点描述，无法校验可过滤字段: {context.tool_name}",
                detail={"tool": context.tool_name, "branch": "missing_endpoint"},
            )

        allowed = tool.endpoint.filterable_fields
        field_types = {field.name: field.data_type for field in tool.endpoint.fields}
        for condition in filter_expr.conditions:
            if condition.field not in allowed:
                return GuardResult.block(
                    f"字段 {condition.field} 不在 {tool.endpoint.form_id} 可过滤字段白名单内",
                    detail={
                        "tool": context.tool_name,
                        "field": condition.field,
                        "allowed_fields": sorted(allowed),
                    },
                )
            type_error = _check_value_type(condition, field_types[condition.field])
            if type_error is not None:
                return GuardResult.block(
                    f"过滤值类型不匹配: {condition.field}（{type_error}）",
                    detail={
                        "tool": context.tool_name,
                        "field": condition.field,
                        "data_type": field_types[condition.field],
                        "reason": type_error,
                    },
                )

        return GuardResult.allow(
            detail={
                "tool": context.tool_name,
                "condition_count": len(filter_expr.conditions),
                "fields": [condition.field for condition in filter_expr.conditions],
            }
        )


def _check_value_type(condition: FilterCondition, data_type: FieldType) -> str | None:
    """value 类型与字段 data_type 的匹配校验；返回拒绝原因，None = 通过。"""
    value = condition.value
    if condition.op is FilterOp.LIKE:
        # LIKE 只对文本类字段有意义，且值必须是字符串
        if data_type == "number":
            return "LIKE 不能用于数值字段"
        if not isinstance(value, str):
            return "LIKE 条件的值必须是字符串"
        return None
    if data_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"数值字段的过滤值必须是数值: {value!r}"
        return None
    if not isinstance(value, str):
        return f"文本类字段的过滤值必须是字符串: {value!r}"
    if data_type == "date" and not _DATE_VALUE_RE.match(value):
        return f"日期字段的过滤值须为 YYYY-MM-DD: {value!r}"
    return None
