"""租户路径命名空间与上下文（M9-T11）。

设计：
- ``default`` 租户沿用旧路径（兼容单租户）；命名租户统一进入 ``tenants/{id}/``
- 任何路径拼接前先校验租户 ID，杜绝 ``../`` 路径穿越
- contextvar 供审计/成本等旁路代码读取当前租户，避免层层传参
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings
from infrastructure.exceptions import TenantError
from models.tenant import DEFAULT_TENANT, TENANT_ID_PATTERN

_current_tenant: ContextVar[str] = ContextVar("kingpilot_tenant", default=DEFAULT_TENANT)


def validate_tenant_id(tenant_id: str) -> str:
    """校验并归一化租户 ID；空/非法一律拒绝（默认拒绝）。"""
    value = (tenant_id or "").strip()
    if not re.fullmatch(TENANT_ID_PATTERN, value):
        raise TenantError("租户 ID 非法或缺失", code="tenant_invalid")
    return value


def current_tenant() -> str:
    """当前上下文租户；未设置时为 ``default``。"""
    return _current_tenant.get()


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    """设置当前租户，退出时恢复；用于请求/一次 Agent 运行。"""
    validated = validate_tenant_id(tenant_id)
    token = _current_tenant.set(validated)
    try:
        yield validated
    finally:
        _current_tenant.reset(token)


def tenant_dir(base: Path, tenant_id: str) -> Path:
    """目录型存储：default 原路径，命名租户加 ``tenants/{id}``。"""
    tenant = validate_tenant_id(tenant_id)
    return base if tenant == DEFAULT_TENANT else base / "tenants" / tenant


def tenant_file(base_file: Path, tenant_id: str) -> Path:
    """文件型存储（audit/ledger）：default 原文件，命名租户换目录同名文件。"""
    tenant = validate_tenant_id(tenant_id)
    if tenant == DEFAULT_TENANT:
        return base_file
    return base_file.parent / "tenants" / tenant / base_file.name


@dataclass(frozen=True, slots=True)
class TenantPaths:
    """一个租户的全部存储路径。"""

    tenant_id: str
    session_root: Path
    trace_dir: Path
    audit_path: Path
    ledger_path: Path
    sample_dir: Path
    approval_dir: Path
    badcase_dir: Path


def tenant_paths(settings: Settings, tenant_id: str) -> TenantPaths:
    """按 Settings 基路径 + 租户 ID 计算五类存储（外加审批/Badcase）。"""
    tenant = validate_tenant_id(tenant_id)
    return TenantPaths(
        tenant_id=tenant,
        session_root=tenant_dir(settings.session_store_dir, tenant),
        trace_dir=tenant_dir(settings.trace_store_dir, tenant),
        audit_path=tenant_file(settings.audit_log_path, tenant),
        ledger_path=tenant_file(settings.ledger_db_path, tenant),
        sample_dir=tenant_dir(settings.online_sample_dir, tenant),
        approval_dir=tenant_dir(settings.approval_store_dir, tenant),
        badcase_dir=tenant_dir(settings.badcase_store_dir, tenant),
    )
