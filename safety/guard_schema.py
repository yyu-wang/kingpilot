"""Guard 1（M4-T02）：Tool 名白名单 + 必填参数 + 类型校验。

方案 §5.1 五层 Guard 的第一层：LLM 产出的 Tool Call 先过「形状关」——
工具是否存在、必填参数是否齐全、参数类型/取值域是否合法。

实现决策：
- **不引入 jsonschema 依赖**：``ToolSpec.parameters`` 本身就是
  ``params_model.model_json_schema()`` 的产物，这里直接复用同一个参数模型做
  ``model_validate``——schema 与校验器同源，杜绝「文档与实现漂移」。
- **保留 Pydantic 宽松数字转换**（``"50"`` → 50）：转换后是强类型，且下游
  ``ToolDefinition.execute`` 会用同一模型复核，不构成越权；明显类型错误
  （``"abc"`` / 数组 / 未知参数）一律拦截。
- **职责边界**：只管形状；空条件查询归 T05，实体存在性归 T04。
- 只捕获预期异常（ToolError / ValidationError），其它异常交给 Guard 管道
  按 fail-closed 统一拦截。
"""

from typing import Any

from pydantic import ValidationError

from agent.tools import ToolRegistry
from infrastructure.exceptions import ToolError
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_schema"

# 单次拦截最多回报的错误条数：够 LLM 纠错，又不让审计日志爆炸
_MAX_REPORTED_ERRORS = 3


class SchemaGuard:
    """Guard 1：工具白名单与参数 Schema 校验（无状态，只读注册表）。"""

    name = GUARD_NAME

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    async def check(self, context: GuardContext) -> GuardResult:
        """校验工具名与参数；不通过时返回带结构化明细的拦截结果。"""
        try:
            tool = self._tools.get(context.tool_name)
        except ToolError:
            return GuardResult.block(
                f"工具 {context.tool_name} 未注册（不在工具白名单内）",
                detail={
                    "tool": context.tool_name,
                    # 供后续把可用工具回喂 LLM 纠错（方案 §5.1 防幻觉闭环）
                    "known_tools": sorted(spec.name for spec in self._tools.all_specs()),
                },
            )

        try:
            tool.params_model.model_validate(dict(context.arguments))
        except ValidationError as exc:
            errors = _validation_errors(exc)
            reason = "；".join(f"{error['field']}: {error['message']}" for error in errors)
            return GuardResult.block(
                f"工具参数校验失败: {context.tool_name}（{reason}）",
                detail={"tool": context.tool_name, "errors": errors},
            )

        return GuardResult.allow(
            detail={"tool": context.tool_name, "param_count": len(context.arguments)}
        )


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic 错误 → 审计友好的结构化条目（只留字段/类型/消息，不回显参数值）。"""
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "type": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors()[:_MAX_REPORTED_ERRORS]
    ]
