"""租户配置表（M9-T11）。

规则与逻辑分离：新增租户/调整预算/指定灰度键只改本文件。
未声明的租户使用默认预算，并以自身 tenant_id 作为 Prompt 灰度键。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TenantProfile:
    """单租户的可配置项。"""

    tenant_id: str
    llm_budget_usd: float | None = None
    prompt_rollout_key: str | None = None


#: 租户白名单与覆盖项；新增租户时在此登记（API Key 身份仍需单独配置）
TENANT_PROFILES: dict[str, TenantProfile] = {
    "default": TenantProfile(tenant_id="default"),
}


def tenant_profile(tenant_id: str) -> TenantProfile:
    """取租户配置；未声明租户返回默认覆盖（预算走全局 Settings）。"""
    return TENANT_PROFILES.get(tenant_id, TenantProfile(tenant_id=tenant_id))
