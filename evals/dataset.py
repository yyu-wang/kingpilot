"""Tool 选对率评测的固定问题集（M2-T05）。

Why Python 常量而非 JSON：期望值引用 TOOL binding 常量（工具改名自动跟随），
mypy strict 覆盖、IDE 可跳转——这不是同义反复：判定对比的是 LLM 输出与常量，
LLM 看不到常量，测试有效性不受影响（T06a 教训的边界辨析）。

集器设计：
- 31 条，5 个查询工具各有 ≥4 条正向覆盖 + 4 条纯聊天（期望不调工具）
- 「参数陷阱」题（工具应选对、参数未必放得下）保留——它们是参数通过率的
  观察样本，不扣选对率分
- 不放写操作陷阱题（如「帮我新建订单」）：当前只有查询工具，无正确答案
  的题硬算选错是指标污染，护栏拦截归 M4 评测
"""

from dataclasses import dataclass

from kingdee.endpoints import (
    CUSTOMER_TOOL,
    INVENTORY_TOOL,
    PURCHASE_ORDER_TOOL,
    SAL_ORDER_TOOL,
    SUPPLIER_TOOL,
)

# 期望值统一引用 binding 常量（见模块 docstring 的 Why）
_SAL = SAL_ORDER_TOOL.tool_name
_CUS = CUSTOMER_TOOL.tool_name
_SUP = SUPPLIER_TOOL.tool_name
_PUR = PURCHASE_ORDER_TOOL.tool_name
_INV = INVENTORY_TOOL.tool_name


@dataclass(frozen=True)
class EvalQuestion:
    """评测集单题：问题 + 期望工具（None = 期望纯聊天、不调任何工具）。"""

    question_id: str  # "SAL-01" 风格，报告里定位用
    question: str
    expected_tool: str | None
    category: str  # 正向 / 参数变体 / 纯聊天 / 参数陷阱


QUESTIONS: tuple[EvalQuestion, ...] = (
    # ── 销售订单（8）──
    EvalQuestion("SAL-01", "帮我查一下最近 5 笔销售订单", _SAL, "参数变体"),
    EvalQuestion("SAL-02", "查一下客户 CUS001 的销售订单", _SAL, "参数变体"),
    EvalQuestion("SAL-03", "有没有金额超过 10 万的销售订单？", _SAL, "参数变体"),
    EvalQuestion("SAL-04", "列出所有已审核状态的销售订单", _SAL, "参数变体"),
    EvalQuestion("SAL-05", "查一下 SO000008 这张销售订单", _SAL, "正向"),
    EvalQuestion("SAL-06", "查一下 2026 年 3 月 5 日的销售订单", _SAL, "参数变体"),
    EvalQuestion("SAL-07", "客户 CUS002 在 2026 年 2 月 2 日的订单有哪些？", _SAL, "参数变体"),
    EvalQuestion("SAL-08", "所有暂存状态的销售订单有哪些？", _SAL, "参数变体"),
    # ── 客户（5）──
    EvalQuestion("CUS-01", "查一下客户 CUS001 的详细信息", _CUS, "参数变体"),
    EvalQuestion("CUS-02", "我们有哪些客户？", _CUS, "正向"),
    EvalQuestion("CUS-03", "有没有名称带「测试」的客户？", _CUS, "参数陷阱"),
    EvalQuestion("CUS-04", "列出前 3 个客户", _CUS, "参数变体"),
    EvalQuestion("CUS-05", "客户简称是「测试一」的是谁？", _CUS, "参数陷阱"),
    # ── 供应商（4）──
    EvalQuestion("SUP-01", "查一下供应商 SUP001 的信息", _SUP, "参数变体"),
    EvalQuestion("SUP-02", "我们合作哪些供应商？", _SUP, "正向"),
    EvalQuestion("SUP-03", "供应商名称带「钢材」的有哪些？", _SUP, "参数陷阱"),
    EvalQuestion("SUP-04", "列出 5 个供应商", _SUP, "参数变体"),
    # ── 采购订单（5）──
    EvalQuestion("PUR-01", "查一下最近 3 笔采购订单", _PUR, "参数变体"),
    EvalQuestion("PUR-02", "供应商 SUP001 的采购订单有哪些？", _PUR, "参数变体"),
    EvalQuestion("PUR-03", "金额超过 5 万的采购订单有哪些？", _PUR, "参数变体"),
    EvalQuestion("PUR-04", "查一下采购订单 PO000001 的详情", _PUR, "参数陷阱"),
    EvalQuestion("PUR-05", "所有已审核的采购订单", _PUR, "参数变体"),
    # ── 库存（5）──
    EvalQuestion("INV-01", "MAT001 的库存还有多少？", _INV, "参数变体"),
    EvalQuestion("INV-02", "查一下 MAT003 在各个仓库的库存分布", _INV, "正向"),
    EvalQuestion("INV-03", "MAT005 的库存够不够 10 个？", _INV, "正向"),
    EvalQuestion("INV-04", "原料仓里存了哪些物料？", _INV, "参数陷阱"),
    EvalQuestion("INV-05", "所有物料的库存清单", _INV, "正向"),
    # ── 纯聊天（4）：期望不调任何工具——LLM 自律性是 Phase 1 的重要指标 ──
    EvalQuestion("CHAT-01", "你好，你是谁？", None, "纯聊天"),
    EvalQuestion("CHAT-02", "你能帮我做什么？", None, "纯聊天"),
    EvalQuestion("CHAT-03", "今天天气怎么样？", None, "纯聊天"),
    EvalQuestion("CHAT-04", "谢谢，辛苦了", None, "纯聊天"),
)
