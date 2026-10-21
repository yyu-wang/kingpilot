"""本地 K3Cloud 仿真沙箱的默认连接参数（仅 dev / 本地演示用）。

这些值是**占位凭证**，与真实金蝶环境无关：
- ``dev`` 环境且未通过环境变量配置金蝶凭证时，API 自动回落到本文件，
  避免本地联调被第三方系统参数阻塞。
- ``prod`` 环境或已显式配置 ``KINGPILOT_KINGDEE_*`` 时**不会**使用本文件。
"""

LOCAL_SANDBOX_URL = "http://127.0.0.1:8000"
LOCAL_SANDBOX_ACCT_ID = "sandbox_acct"
LOCAL_SANDBOX_USER = "demo"
LOCAL_SANDBOX_APP_ID = "sandbox_app"
LOCAL_SANDBOX_APP_SECRET = "sandbox_secret_not_a_real_credential"  # pragma: allowlist secret

__all__ = [
    "LOCAL_SANDBOX_ACCT_ID",
    "LOCAL_SANDBOX_APP_ID",
    "LOCAL_SANDBOX_APP_SECRET",
    "LOCAL_SANDBOX_URL",
    "LOCAL_SANDBOX_USER",
]
