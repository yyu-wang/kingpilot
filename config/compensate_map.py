"""M5-T02 · Saga 补偿映射表（方案 §5.1 支柱三）。

运行时只读。键为 ``(tool_name, action)``，值为补偿工具名。
真实写工具尚未落地，这里先预置方案里的示例映射；测试会注入自定义映射。
"""

COMPENSATE_MAP: dict[tuple[str, str], str] = {
    ("kingdee_create_sales_order", "save"): "kingdee_delete_sales_order",
    ("kingdee_submit_sales_order", "submit"): "kingdee_unsubmit_sales_order",
    ("kingdee_audit_sales_order", "audit"): "kingdee_unaudit_sales_order",
    ("kingdee_create_purchase_in", "save"): "kingdee_delete_purchase_in",
}

# 无自动补偿的操作（已产生不可逆业务后果），必须人工介入
NO_AUTO_COMPENSATE: frozenset[str] = frozenset(
    {
        "kingdee_audit_stock_out",
        "kingdee_audit_finance_voucher",
    }
)
