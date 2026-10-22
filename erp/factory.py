"""ERP 客户端工厂：按 ``settings.erp_vendor`` 选择厂商适配器。

Why 工厂放在 ``erp/``：
- API / CLI / 运维脚本三个入口都要构造客户端，散落会导致新增厂商时漏改；
- 它正是「新增厂商三步」的最后一步（实现协议 → 声明端点 → 工厂注册）。

Why 厂商适配器用**函数内导入**而非模块级导入：
``erp/`` 是厂商中立契约层。模块级 ``from kingdee.client import KingdeeClient``
会形成 ``erp.factory → kingdee.client → erp.types`` 的导入期循环，并让契约层
反向依赖厂商。延迟导入把依赖限制在调用期，模块级依赖图保持单向：
``agent / safety / api → erp``、``kingdee → erp``。

Why 不用注册表（``register_vendor``）：
注册表需要某个入口先执行注册副作用，漏注册就退化成「厂商不存在」且难排查；
当前只有一个真实实现 + 一个骨架，显式分支更可读、失败更直接（YAGNI）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from erp.protocol import ErpClient
from infrastructure.exceptions import ConfigurationError

if TYPE_CHECKING:
    from config.settings import Settings
    from infrastructure.secrets import SecretProvider

__all__ = ["SUPPORTED_VENDORS", "build_erp_client"]

#: 已支持的厂商标识（新增厂商时同步此列表与 build_erp_client 分支）
SUPPORTED_VENDORS: tuple[str, ...] = ("kingdee", "yonyou")


def build_erp_client(settings: Settings, *, secrets: SecretProvider | None = None) -> ErpClient:
    """按配置构造 ERP 客户端；未知厂商抛 ``ConfigurationError``（启动期 fail-fast）。

    Args:
        settings: 全局配置（读取 ``erp_vendor`` 与厂商专属连接参数）。
        secrets: 密钥提供者；``None`` 时由适配器自行按 ``secret_provider`` 构造。

    Raises:
        ConfigurationError: ``erp_vendor`` 不在 ``SUPPORTED_VENDORS`` 内。
    """
    vendor = settings.erp_vendor.strip().lower()
    if vendor == "kingdee":
        from kingdee.client import KingdeeClient

        return KingdeeClient(settings, secrets=secrets)

    if vendor == "yonyou":
        from erp.yonyou.client import YonyouClient

        # 骨架：构造成功但 query/invoke_action 会明确报「未接入」
        return YonyouClient(settings, secrets=secrets)

    raise ConfigurationError(
        f"不支持的 ERP 厂商: {settings.erp_vendor}",
        context={"erp_vendor": settings.erp_vendor, "supported": list(SUPPORTED_VENDORS)},
    )
