"""支柱一 · 风险规则表（占位，M4 填充）。

对应方案 §17 支柱一：所有危险动作拦截在此表定义，运行时**只读**。

规则字段（M4 定稿，此处仅占位）：
    rule_id       规则唯一标识
    tool_name     匹配的工具名（支持通配）
    risk_level    风险等级（low/medium/high/critical）
    action        拦截动作（allow/deny/require_approval）
    message       触发时的人类可读提示

M4-T10 之前保持空表：默认放行由 SafetyEngine 显式兜底，避免误伤。
"""

from typing import Any

RISK_RULES: list[dict[str, Any]] = []
