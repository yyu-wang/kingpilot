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
from typing import Any

from agent.knowledge import KB_SEARCH_TOOL
from erp.tool_names import (
    OBJ_PURCHASE_INBOUND,
    OBJ_PURCHASE_ORDER,
    OBJ_SALES_ORDER,
    QUERY_CUSTOMER,
    QUERY_INVENTORY,
    QUERY_PURCHASE_INBOUND,
    QUERY_PURCHASE_ORDER,
    QUERY_SALES_ORDER,
    QUERY_SUPPLIER,
    write_tool_name,
)
from models.llm import PlanResult, ToolCallDecision

SALES_ORDER_TOOL = QUERY_SALES_ORDER
PURCHASE_ORDER_TOOL = QUERY_PURCHASE_ORDER
PURCHASE_INBOUND_TOOL = QUERY_PURCHASE_INBOUND
CUSTOMER_TOOL = QUERY_CUSTOMER
SUPPLIER_TOOL = QUERY_SUPPLIER
INVENTORY_TOOL = QUERY_INVENTORY

_SO_RE = re.compile(r"\b(SO\d+)\b", re.IGNORECASE)
_PO_RE = re.compile(r"\b(PO\d+)\b", re.IGNORECASE)
_CGRK_RE = re.compile(r"\b(CGRK\d+)\b", re.IGNORECASE)
_CUS_RE = re.compile(r"\b(CUS\d+)\b", re.IGNORECASE)
_SUP_RE = re.compile(r"\b(SUP\d+)\b", re.IGNORECASE)
_MAT_RE = re.compile(r"\b(MAT\d+)\b", re.IGNORECASE)
_CODE_LIKE_RE = re.compile(r"^(?:SO|PO|CGRK|CUS|SUP|MAT)\d+$", re.IGNORECASE)

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
# 「可以/能否/是否」覆盖「优惠券可以叠加使用吗」这类规则咨询句，避免模型不检索就作答。
# 「怎么」排除「怎么样」：天气/闲聊类问题不能被当成知识咨询去检索。
_KNOWLEDGE_KEYWORD_RE = re.compile(
    r"怎么(?!样)|如何|流程|规范|手册|制度|步骤|说明|可以|能不能|能否|是否"
)
_BUSINESS_CODE_RE = re.compile(r"\b(?:SO|PO|CGRK|CUS|SUP|MAT)\d+\b", re.IGNORECASE)

# 订单明细意图：LLM 宕机/空规划时也要把明细行查出来，而不是只给表头
_MATERIAL_DETAIL_KEYWORDS = ("物料", "明细", "分录", "数量", "单价")


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

    def match_tool_scope(self, user_input: str) -> str | None:
        """仅推断请求指向的查询工具（不构造过滤参数），供权限预检使用。

        Why 与 ``match`` 分开：权限判定只关心「用户想查哪类数据」，
        不需要可执行参数——「查一下采购订单」这类无过滤条件的请求
        也能直接识别出目标数据域并给出权限提示。
        """
        text = user_input.strip()
        if not text or _is_write_intent(text) or _is_knowledge_intent(text):
            return None
        if "采购入库" in text or "入库单" in text or _CGRK_RE.search(text):
            return PURCHASE_INBOUND_TOOL
        if "采购" in text or _PO_RE.search(text):
            return PURCHASE_ORDER_TOOL
        if "销售" in text or "订单" in text or _SO_RE.search(text):
            return SALES_ORDER_TOOL
        if "客户" in text:
            return CUSTOMER_TOOL
        if "供应商" in text:
            return SUPPLIER_TOOL
        if "库存" in text:
            return INVENTORY_TOOL
        return None

    def match_write_tool_scope(self, user_input: str) -> str | None:
        """推断写意图指向的写工具（不看参数），供写权限预检使用。

        写工具被数据域过滤后不会下发给 LLM，模型可能直接编造「已创建/已删除」。
        这里先按关键词识别目标写工具，再交给权限引擎判断，确保无权限时
        直接返回「权限不足」，而不是让模型自由发挥。
        """
        text = user_input.strip()
        if not text:
            return None
        # 「做入库」「登记入库」不在通用写关键词里，但属于创建入库单的明确意图
        if not _is_write_intent(text) and not any(
            keyword in text for keyword in ("做入库", "登记入库")
        ):
            return None
        action = _extract_write_action(text)
        if action is None:
            return None
        for keyword, form in _WRITE_FORM_KEYWORDS:
            if keyword in text:
                return write_tool_name(form, action)
        return None

    def match_write_plan(
        self, user_input: str, *, available_tools: Collection[str] | None = None
    ) -> FallbackPlan | None:
        """「动作开头 + 明确单号」的状态流转意图 → 写工具调用（零 LLM）。

        Why：多轮上下文里模型可能漏掉写工具、直接回答「已提交审批」，而实际
        没有审批单（经理账号看不到卡片）。这里只补全参数可确定的状态流转类
        动作（提交/审核/反审核/删除，仅需 bill_no）；创建类参数多，仍交给 LLM。
        补全的计划继续走安全审查与审批，不跳过任何护栏。
        """
        text = user_input.strip()
        leading = _LEADING_WRITE_ACTION_RE.match(text)
        if leading is None:
            return None
        action = _WRITE_ACTION_BY_WORD[leading.group("action")]
        if action not in _BILL_ONLY_ACTIONS:
            return None
        bills = [(form, _extract(pattern, text)) for form, pattern in _BILL_NO_PATTERNS]
        candidates = [(form, bill_no) for form, bill_no in bills if bill_no is not None]
        if len(candidates) != 1:
            return None
        bill_form, bill_no = candidates[0]
        keyword_form = next(
            (form for keyword, form in _WRITE_FORM_KEYWORDS if keyword in text),
            None,
        )
        # 「反审核销售订单 PO000001」这类单据类型与单号前缀矛盾的输入不猜
        if keyword_form is not None and keyword_form != bill_form:
            return None
        tool_name = write_tool_name(bill_form, action)
        if tool_name is None:
            return None
        return _build(
            tool_name,
            {"bill_no": bill_no},
            f"write_{action}",
            available_tools,
        )

    def match_knowledge_plan(
        self, user_input: str, *, available_tools: Collection[str] | None = None
    ) -> FallbackPlan | None:
        """知识意图且 LLM 未调工具时，补一次 ``kb_search``。

        Why：模型可能凭常识回答流程/时效问题，绕过知识库导致没有引用。
        这里只补检索，不生成内容；引用仍由 Phase 4.5 强制校验。
        """
        text = user_input.strip()
        if not text or not _is_knowledge_intent(text):
            return None
        return _build(
            KB_SEARCH_TOOL,
            {"query": text[:200]},
            "knowledge",
            available_tools,
        )

    def _sales_plan(
        self, text: str, available_tools: Collection[str] | None
    ) -> FallbackPlan | None:
        arguments: dict[str, Any] = {}
        bill_no = _extract(_SO_RE, text)
        if bill_no is not None:
            arguments["bill_no"] = bill_no
        customer_number = _extract(_CUS_RE, text)
        if customer_number is not None:
            arguments["customer_number"] = customer_number
        status = _extract_status(text)
        if status is not None:
            arguments["document_status"] = status
        if any(keyword in text for keyword in _MATERIAL_DETAIL_KEYWORDS):
            arguments["include_entries"] = True
        if not arguments:
            return None
        return _build(SALES_ORDER_TOOL, arguments, "sales_order", available_tools)

    def _purchase_plan(
        self, text: str, available_tools: Collection[str] | None
    ) -> FallbackPlan | None:
        arguments: dict[str, Any] = {}
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
    arguments: dict[str, Any],
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


# 写意图 → 单据类型（顺序重要：「入库」优先于「采购」）
_WRITE_FORM_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("入库", OBJ_PURCHASE_INBOUND),
    ("采购", OBJ_PURCHASE_ORDER),
    ("销售", OBJ_SALES_ORDER),
)

# 单号前缀 → 单据类型（用于「动作 + 单号」的确定性补全）
_BILL_NO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (OBJ_PURCHASE_INBOUND, _CGRK_RE),
    (OBJ_SALES_ORDER, _SO_RE),
    (OBJ_PURCHASE_ORDER, _PO_RE),
)

# 只补全「仅需单号」的状态流转动作；创建类参数多，仍交给 LLM
_BILL_ONLY_ACTIONS = frozenset({"submit", "audit", "unaudit", "delete"})

# 动作必须出现在句首（允许「请/麻烦/帮我」前缀）：避免把
# 「SO900001 审核了吗」这类询问误判为写操作
_LEADING_WRITE_ACTION_RE = re.compile(
    r"^\s*(?:请|麻烦)?\s*(?:帮我)?\s*(?P<action>反审核|审核|提交|删除|作废)"
)
_WRITE_ACTION_BY_WORD: dict[str, str] = {
    "反审核": "unaudit",
    "审核": "audit",
    "提交": "submit",
    "删除": "delete",
    "作废": "delete",
}


def _extract_write_action(text: str) -> str | None:
    """写意图 → 工具名动作段（创建类工具名用 create，不是 action 的 save）。"""
    if "反审核" in text:
        return "unaudit"
    if "审核" in text:
        return "audit"
    if "删除" in text or "作废" in text:
        return "delete"
    if "提交" in text:
        return "submit"
    if any(
        keyword in text
        for keyword in ("创建", "新建", "新增", "开一张", "做一张", "做入库", "登记入库")
    ):
        return "create"
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
    return _KNOWLEDGE_KEYWORD_RE.search(text) is not None
