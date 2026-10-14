"""支柱五 · 权限规则表（占位，M5 填充）。

对应方案 §17 支柱五：四层权限隔离（user / agent / tool / data），运行时**只读**。

规则字段（M5 定稿，此处仅占位）：
    role          角色（admin/operator/viewer/...）
    allow_tools   允许调用的工具白名单
    deny_tools    显式禁止的工具黑名单
    data_scope    数据可见范围（如组织/单据类型）

M5 之前保持空表：PermissionEngine 默认拒绝敏感工具（fail-closed）。
"""

from typing import Any

PERMISSION_RULES: list[dict[str, Any]] = []
