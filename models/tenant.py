"""租户上下文（M9-T11）。

租户 ID 是存储路径与资源隔离的唯一键：
- 只允许小写字母/数字开头，后续可含 ``._-``，长度 1~32
- 默认租户 ``default`` 沿用旧路径（兼容单租户）
"""

from typing import TYPE_CHECKING

from pydantic import Field

from models.base import KingPilotBaseModel

if TYPE_CHECKING:
    from models.user import UserContext

TENANT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,31}$"
DEFAULT_TENANT = "default"


class TenantContext(KingPilotBaseModel):
    """一次请求所属的租户。"""

    tenant_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=TENANT_ID_PATTERN,
        description="租户 ID（小写字母/数字开头）",
    )

    @classmethod
    def from_user(cls, user: "UserContext") -> "TenantContext":
        """从用户上下文提取租户；空值会被 Pydantic 拒绝（默认拒绝）。"""
        return cls(tenant_id=user.tenant_id)
