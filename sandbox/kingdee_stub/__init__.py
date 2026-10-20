"""K3Cloud 契约仿真沙箱（M1-T06a，测试设施，不进 wheel 打包范围）。

对外只需 ``from sandbox.kingdee_stub import app``（ASGI 应用）或
``from sandbox import sandbox_settings``（指向沙箱的配置工厂）。
"""

from sandbox.kingdee_stub.app import (
    AUDIT_PATH,
    DELETE_PATH,
    LOGIN_PATH,
    QUERY_PATH,
    SANDBOX_ACCT_ID,
    SANDBOX_APP_ID,
    SANDBOX_APP_SECRET,
    SANDBOX_BASE_PATH,
    SANDBOX_USER,
    SAVE_PATH,
    SESSION_COOKIE_NAME,
    SUBMIT_PATH,
    UNAUDIT_PATH,
    app,
    reset_sessions,
)
from sandbox.kingdee_stub.inbound_store import reset_purchase_inbound_dataset
from sandbox.kingdee_stub.purchase_order_store import reset_purchase_order_dataset
from sandbox.kingdee_stub.store import reset_dataset as reset_sales_dataset


def reset_dataset() -> None:
    """复位全部可变单据库（销售订单 + 采购订单 + 采购入库单）。"""
    reset_sales_dataset()
    reset_purchase_order_dataset()
    reset_purchase_inbound_dataset()


__all__ = [
    "AUDIT_PATH",
    "DELETE_PATH",
    "LOGIN_PATH",
    "QUERY_PATH",
    "SANDBOX_ACCT_ID",
    "SANDBOX_APP_ID",
    "SANDBOX_APP_SECRET",
    "SANDBOX_BASE_PATH",
    "SANDBOX_USER",
    "SAVE_PATH",
    "SESSION_COOKIE_NAME",
    "SUBMIT_PATH",
    "UNAUDIT_PATH",
    "app",
    "reset_dataset",
    "reset_sessions",
]
