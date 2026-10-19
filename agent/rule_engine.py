"""只读降级规则引擎（M6-T01，方案 §5.2 支柱六）。

LLM 不可用时，用正则/关键词把常见查询意图映射为固定查询工具调用：
- 销售订单：SO 编号 / CUS 客户编码 / 状态
- 采购订单：SUP 供应商编码 / 状态
- 客户 / 供应商：名称模糊
- 即时库存：MAT 物料编码

红线：
- **只读**：写操作意图（创建/修改/删除/提交/审核）一律不匹配
- 不猜无条件查询：没有任何过滤条件时返回 None（避免被 Guard 4 拦截）
- 输出仍是 ``PlanResult``，继续走 M4 安全审查与 M5 执行链路
"""

import re
from collections.abc import Collection
from dataclasses import dataclass

from models.llm import PlanResult, ToolCallDecision

SALES_ORDER_TOOL = "kingdee_query_sales_order"
PURCHASE_ORDER_TOOL = "kingdee_query_purchase_order"
CUSTOMER_TOOL = "kingdee_query_customer"
SUPPLIER_TOOL = "kingdee_query_supplier"
INVENTORY_TOOL = "kingdee_query_inventory"

_SO_RE = re.compile(r"\b(SO\d+)\b", re.IGNORECASE)
_CUS_RE = re.compile(r"\b(CUS\d+)\b", re.IGNORECASE)
_SUP_RE = re.compile(r"\b(SUP\d+)\b", re.IGNORECASE)
_MAT_RE = re.compile(r"\b(MAT\d+)\b", re.IGNORECASE)
_CODE_LIKE_RE = re.compile(r"^(?:SO|CUS|SUP|MAT)\d+$", re.IGNORECASE)

_CUSTOMER_NAME_RE = re.compile(
    r"(?:查|查询|查看|看)?\s*客户(?:资料|信息)?[:：\s]*(?P<name>[\u4e00-\u9fa5A-Za-z0-9_]+)"
)
_SUPPLIER_NAME_RE = re.compile(
    r"(?:查|查询|查看|看)?\s*供应商(?:资料|信息)?[:：\s]*(?P<name>[\u4e00-\u9fa5A-Za-z0-9_]+)"
)

# 顺序重要：先匹配长词（重新审核 > 审核中 > 已审核 ...）
_STATUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("重新审核", "D"),
    ("审核中", "B"),
    ("已审核", "C"),
    ("未审核", "A"),
    ("待审核", "A"),
    ("暂存", "Z"),
)

# 写操作关键词：命中即拒绝降级（写入不降级）
_WRITE_KEYWORDS = (
    "创建",
    "新建",
    "保存",
    "修改",
    "编辑",
    "删除",
    "提交",
    "反审核",
    "改成",
    "改为",
)
_READ_STATUS_WORDS = ("已审核", "未审核", "待审核", "审核中", "重新审核")

# 知识意图关键词（M9-T09）：LLM 宕机时不要把「怎么做/流程/规范」误降级成 ERP 查询
_KNOWLEDGE_KEYWORDS = ("怎么", "如何", "流程", "规范", "手册", "制度", "步骤", "说明")
_BUSINESS_CODE_RE = re.compile(r"\b(?:SO|CUS|SUP|MAT)\d+\b", re.IGNORECASE)


@dataclass(frozen=True)
class FallbackPlan:
    """规则引擎命中的只读计划（供主循环记录 intent）。"""

    intent: str
    plan: PlanResult


class RuleEngine:
    """常见查询意图 → 固定 Tool 映射（零 LLM）。"""

    def match(
        self,
        user_input: str,
        *,
        available_tools: Collection[str] | None = None,
    ) -> FallbackPlan | None:
        """匹配只读查询；无匹配或写意图返回 None。"""
        text = user_input.strip()
        if not text or _is_write_intent(text):
            return None
        if _is_knowledge_intent(text):
            # 知识问题交给 kb_search；LLM 不可用时宁可不答，也不误查 ERP
            return None

        is_purchase = "采购" in text
        is_sales = not is_purchase and ("销售" in text or "订单" in text)

        if is_purchase:
            return self._purchase_plan(text, available_tools)
        if is_sales:
            return self._sales_plan(text, available_tools)

        inventory = _extract(_MAT_RE, text)
        if inventory is not None and ("库存" in text or "物料" in text):
            return _build(
                INVENTORY_TOOL,
                {"material_number": inventory},
                "inventory",
                available_tools,
            )

        customer_name = _extract_name(_CUSTOMER_NAME_RE, text)
        if customer_name is not None:
            return _build(CUSTOMER_TOOL, {"name": customer_name}, "customer", available_tools)

        supplier_name = _extract_name(_SUPPLIER_NAME_RE, text)
        if supplier_name is not None:
            return _build(SUPPLIER_TOOL, {"name": supplier_name}, "supplier", available_tools)

        return None

    def _sales_plan(
        self, text: str, available_tools: Collection[str] | None
    ) -> FallbackPlan | None:
        arguments: dict[str, str] = {}
        bill_no = _extract(_SO_RE, text)
        if bill_no is not None:
            arguments["bill_no"] = bill_no
        customer_number = _extract(_CUS_RE, text)
        if customer_number is not None:
            arguments["customer_number"] = customer_number
        status = _extract_status(text)
        if status is not None:
            arguments["document_status"] = status
        if not arguments:
            return None
        return _build(SALES_ORDER_TOOL, arguments, "sales_order", available_tools)

    def _purchase_plan(
        self, text: str, available_tools: Collection[str] | None
    ) -> FallbackPlan | None:
        arguments: dict[str, str] = {}
        supplier_number = _extract(_SUP_RE, text)
        if supplier_number is not None:
            arguments["supplier_number"] = supplier_number
        status = _extract_status(text)
        if status is not None:
            arguments["document_status"] = status
        if not arguments:
            return None
        return _build(PURCHASE_ORDER_TOOL, arguments, "purchase_order", available_tools)


def _build(
    tool_name: str,
    arguments: dict[str, str],
    intent: str,
    available_tools: Collection[str] | None,
) -> FallbackPlan | None:
    if available_tools is not None and tool_name not in available_tools:
        return None
    return FallbackPlan(
        intent=intent,
        plan=PlanResult(
            tool_calls=[
                ToolCallDecision(
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    rationale=f"fallback:{intent}",
                )
            ],
            reasoning=f"规则引擎降级命中：{intent}",
        ),
    )


def _extract(pattern: re.Pattern[str], text: str) -> str | None:
    matched = pattern.search(text)
    return matched.group(1).upper() if matched is not None else None


def _extract_name(pattern: re.Pattern[str], text: str) -> str | None:
    matched = pattern.search(text)
    if matched is None:
        return None
    name = matched.group("name").strip()
    # 纯编码交给订单/库存意图；客户/供应商工具只支持名称模糊查询
    if not name or _CODE_LIKE_RE.match(name):
        return None
    return name


def _extract_status(text: str) -> str | None:
    for keyword, value in _STATUS_KEYWORDS:
        if keyword in text:
            return value
    return None


def _is_write_intent(text: str) -> bool:
    if any(keyword in text for keyword in _WRITE_KEYWORDS):
        return True
    # “审核”只有在不是查询状态词时才视为写操作
    return "审核" in text and not any(word in text for word in _READ_STATUS_WORDS)


def _is_knowledge_intent(text: str) -> bool:
    """知识意图：有「怎么做/流程/规范…」且没有显式业务编码。"""
    if _BUSINESS_CODE_RE.search(text):
        return False
    return any(keyword in text for keyword in _KNOWLEDGE_KEYWORDS)
