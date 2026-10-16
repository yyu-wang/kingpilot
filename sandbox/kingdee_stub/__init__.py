"""K3Cloud 契约仿真沙箱（M1-T06a，测试设施，不进 wheel 打包范围）。

对外只需 ``from sandbox.kingdee_stub import app``（ASGI 应用）或
``from sandbox import sandbox_settings``（指向沙箱的配置工厂）。
"""

from sandbox.kingdee_stub.app import (
    LOGIN_PATH,
    QUERY_PATH,
    SANDBOX_ACCT_ID,
    SANDBOX_APP_ID,
    SANDBOX_APP_SECRET,
    SANDBOX_BASE_PATH,
    SANDBOX_USER,
    SESSION_COOKIE_NAME,
    app,
    reset_sessions,
)

__all__ = [
    "LOGIN_PATH",
    "QUERY_PATH",
    "SANDBOX_ACCT_ID",
    "SANDBOX_APP_ID",
    "SANDBOX_APP_SECRET",
    "SANDBOX_BASE_PATH",
    "SANDBOX_USER",
    "SESSION_COOKIE_NAME",
    "app",
    "reset_sessions",
]
