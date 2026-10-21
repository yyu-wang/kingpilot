"""测试设施包：本地 K3Cloud 契约仿真沙箱（M1-T06a）。

**不进 hatch wheel 打包范围**（见 pyproject ``[tool.hatch.build.targets.wheel]``）：
它是开发/测试设施，不随生产包分发。生产代码永远不会 import ``sandbox``——
契约替身只在开发机与 CI 上跑。
"""

from pydantic import SecretStr

from config.local_sandbox import LOCAL_SANDBOX_URL
from config.settings import Settings
from sandbox.kingdee_stub.app import (
    SANDBOX_ACCT_ID,
    SANDBOX_APP_ID,
    SANDBOX_APP_SECRET,
    SANDBOX_USER,
)

DEFAULT_SANDBOX_URL = LOCAL_SANDBOX_URL


def sandbox_settings(
    server_url: str = DEFAULT_SANDBOX_URL, *, timeout_seconds: float = 5.0
) -> Settings:
    """构造指向本地仿真沙箱的 ``Settings``（M1-T06b 集成验收与手工脚本用）。

    Why 用显式关键字参数：``Settings(**dict[str, object])`` 在 mypy strict 下
    无法通过（无法静态确认键值类型），显式关键字让类型检查器逐参数核对。
    """
    return Settings(
        kingdee_server_url=server_url,
        kingdee_acct_id=SANDBOX_ACCT_ID,
        kingdee_user=SANDBOX_USER,
        kingdee_app_id=SANDBOX_APP_ID,
        kingdee_app_secret=SecretStr(SANDBOX_APP_SECRET),
        kingdee_timeout_seconds=timeout_seconds,
    )


__all__ = ["DEFAULT_SANDBOX_URL", "sandbox_settings"]
