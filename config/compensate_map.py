"""M5-T02 · Saga 补偿映射表（方案 §5.1 支柱三）。

运行时只读。键为 ``(tool_name, action)``，值为补偿工具名。

Why 引用 ``erp.tool_names`` 常量而非字面量：工具名改名时若映射表漏改，
补偿会在运行时静默找不到工具；引用常量则改名即同步，且 ruff/mypy 能发现引用错误。
"""

from erp.tool_names import (
    AUDIT_PURCHASE_INBOUND,
    AUDIT_PURCHASE_ORDER,
    AUDIT_SALES_ORDER,
    CREATE_PURCHASE_INBOUND,
    CREATE_PURCHASE_ORDER,
    CREATE_SALES_ORDER,
    DELETE_PURCHASE_INBOUND,
    DELETE_PURCHASE_ORDER,
    DELETE_SALES_ORDER,
    SUBMIT_PURCHASE_INBOUND,
    SUBMIT_PURCHASE_ORDER,
    SUBMIT_SALES_ORDER,
    UNAUDIT_PURCHASE_INBOUND,
    UNAUDIT_PURCHASE_ORDER,
    UNAUDIT_SALES_ORDER,
)

COMPENSATE_MAP: dict[tuple[str, str], str] = {
    (CREATE_SALES_ORDER, "save"): DELETE_SALES_ORDER,
    (AUDIT_SALES_ORDER, "audit"): UNAUDIT_SALES_ORDER,
    (CREATE_PURCHASE_ORDER, "save"): DELETE_PURCHASE_ORDER,
    (AUDIT_PURCHASE_ORDER, "audit"): UNAUDIT_PURCHASE_ORDER,
    (CREATE_PURCHASE_INBOUND, "save"): DELETE_PURCHASE_INBOUND,
    (AUDIT_PURCHASE_INBOUND, "audit"): UNAUDIT_PURCHASE_INBOUND,
}

# 无自动补偿的操作（已产生不可逆业务后果），必须人工介入。
# 提交后金蝶没有 UnSubmit 动作，撤回只能走删除审批，不能自动补偿。
NO_AUTO_COMPENSATE: frozenset[str] = frozenset(
    {
        SUBMIT_SALES_ORDER,
        SUBMIT_PURCHASE_ORDER,
        SUBMIT_PURCHASE_INBOUND,
    }
)
