"""用户上下文（M0-T06）。

四层权限隔离（方案 §17 支柱五）：
1. 用户层：user_id + role
2. 数据层：data_scope（组织/单据类型白名单）
3. 工具层：allow_tools / deny_tools（M5 填充）
4. 字段层：敏感字段脱敏规则（M5 填充）
"""

from typing import Any

from pydantic import Field

from models.base import KingPilotBaseModel, Role


class UserContext(KingPilotBaseModel):
    """用户上下文（四层权限隔离的载体）。"""

    user_id: str = Field(description="用户唯一标识")
    role: Role = Field(description="角色（admin/operator/viewer）")
    data_scope: dict[str, Any] = Field(
        default_factory=dict,
        description="数据可见范围（如 {'org_ids': ['ORG001'], 'form_ids': ['SAL_Order']}）",
    )
    ip_address: str = Field(default="", description="IP 地址（审计用）")

    # 预留字段（M5 权限引擎填充，本轮占位）
    allow_tools: list[str] = Field(default_factory=list, description="工具白名单（M5 填充）")
    deny_tools: list[str] = Field(default_factory=list, description="工具黑名单（M5 填充）")
