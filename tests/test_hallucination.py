"""M3-T06：幻觉检测器单测（确定性规则，零网络）。

覆盖三重校验的命中与反例（防误报）：
- 数字事实核对：阿拉伯数字、万/亿换算、中文数量词、日期/百分比/序数豁免
- Tool 结果来源：编造标识、用户输入与失败调用参数豁免
- 业务一致性：实体图状态与回复声称不一致
- 验收：注入式幻觉 fixture 检出率 100%
"""

from agent.hallucination import HallucinationDetectionResult, detect_hallucinations
from models.memory import EntityRef, RawMessage
from models.tool import ToolExecution

_ORDER_ROWS: list[dict[str, object]] = [
    {
        "FBillNo": "SO000001",
        "FDeliveryDate": "2026-03-05",
        "FBillAllAmount": "45000.0000000000",
    },
    {
        "FBillNo": "SO000008",
        "FDeliveryDate": "2026-04-01",
        "FBillAllAmount": "256000.0000000000",
    },
]


def _orders_execution(rows: list[dict[str, object]] | None = None) -> ToolExecution:
    return ToolExecution(
        tool_name="kingdee_query_sales_order",
        parameters={"limit": 10},
        result={
            "form_id": "SAL_Order",
            "field_keys": ["FBillNo", "FDeliveryDate", "FBillAllAmount"],
            "rows": _ORDER_ROWS if rows is None else rows,
        },
    )


def _failed_execution(parameters: dict[str, object] | None = None) -> ToolExecution:
    return ToolExecution(
        tool_name="kingdee_query_sales_order",
        parameters=parameters or {},
        error="金蝶业务错误",
    )


def _order_entity(status: str = "C") -> EntityRef:
    return EntityRef(
        entity_type="order",
        display_name="SO000008",
        kingdee_id="SO000008",
        last_mentioned_in_round=1,
        attributes={"FDocumentStatus": status},
    )


# ── Check 1：数字事实核对 ──


def test_number_mismatch_detected() -> None:
    result = detect_hallucinations("查到 5 张订单", [_orders_execution()])
    assert result.has_hallucination is True
    assert result.detection_type == "tool result hallucination"
    item = result.hallucinated_items[0]
    assert item.claimed_value == 5
    assert item.actual_value == [2]
    assert item.severity == "high"


def test_number_from_tool_result_passes() -> None:
    assert detect_hallucinations("共 2 张订单", [_orders_execution()]).has_hallucination is False


def test_unit_conversion_wan_passes() -> None:
    """25.6 万 = 256000，工具返回中确有该金额 → 不误报。"""
    result = detect_hallucinations("合计金额 25.6 万元", [_orders_execution()])
    assert result.has_hallucination is False


def test_unit_conversion_mismatch_detected() -> None:
    result = detect_hallucinations("合计金额 30 万元", [_orders_execution()])
    assert result.has_hallucination is True
    assert result.hallucinated_items[0].claimed_value == "300000"


def test_chinese_count_matches_rows_passes() -> None:
    assert detect_hallucinations("查到两条订单", [_orders_execution()]).has_hallucination is False


def test_chinese_count_mismatch_detected() -> None:
    result = detect_hallucinations("查到三条订单", [_orders_execution()])
    assert result.has_hallucination is True
    assert result.hallucinated_items[0].claimed_value == 3


def test_date_percent_and_ordinal_are_skipped() -> None:
    """日期/百分比/序数不属业务事实断言（口径见模块 docstring），不误报。"""
    response = "在 2026年3月 完成了 30% 的订单，第1笔是 SO000001，第 2 段是审核说明。"
    result = detect_hallucinations(response, [_orders_execution()])
    assert result.has_hallucination is False


def test_number_claim_without_tool_results_detected() -> None:
    result = detect_hallucinations("查到 3 笔订单", [])
    assert result.has_hallucination is True
    assert result.hallucinated_items[0].actual_value == []


def test_thousands_separator_amount_passes() -> None:
    """模型常用千分位金额（150,000.00），必须归一后与工具原值比对。"""
    rows: list[dict[str, object]] = [{"FBillNo": "SO000001", "FBillAllAmount": "150000.0000000000"}]
    result = detect_hallucinations("金额为 150,000.00 元", [_orders_execution(rows)])
    assert result.has_hallucination is False


def test_thousands_separator_mismatch_detected() -> None:
    rows: list[dict[str, object]] = [{"FBillNo": "SO000001", "FBillAllAmount": "150000.0000000000"}]
    result = detect_hallucinations("金额为 160,000.00 元", [_orders_execution(rows)])
    assert result.has_hallucination is True
    assert result.hallucinated_items[0].claimed_value == "160000"


def test_numbered_list_markers_skipped() -> None:
    """「1. 订单...」的行首编号不是业务数字。"""
    response = "查询结果：\n1. 订单 SO000001\n2. 订单 SO000008"
    assert detect_hallucinations(response, [_orders_execution()]).has_hallucination is False


def test_markdown_table_index_column_skipped() -> None:
    """表格首列的连续序号（| 1 | / | 2 |）不是业务数字。"""
    response = "| 序号 | 订单 |\n|---|---|\n| 1 | SO000001 |\n| 2 | SO000008 |"
    assert detect_hallucinations(response, [_orders_execution()]).has_hallucination is False


def test_history_supports_cross_turn_references() -> None:
    """多轮场景：上一轮的用户输入与工具结果都是合法事实来源。"""
    history = [
        RawMessage(role="user", content="查客户 CUS001 的订单"),
        RawMessage(
            role="tool",
            content="",
            structured_result={
                "form_id": "SAL_Order",
                "rows": [{"FBillNo": "SO000012", "FBillAllAmount": "181736.0000000000"}],
            },
        ),
    ]
    result = detect_hallucinations(
        "客户 CUS001 的订单 SO000012 金额为 181,736.00 元",
        [],
        history=history,
    )
    assert result.has_hallucination is False


# ── Check 2：Tool 结果来源 ──


def test_fabricated_identifier_detected() -> None:
    result = detect_hallucinations("订单 SO000999 已审核", [_orders_execution()])
    assert result.has_hallucination is True
    assert result.detection_type == "entity fabrication"
    assert result.hallucinated_items[0].claimed_value == "SO000999"
    assert result.hallucinated_items[0].severity == "critical"


def test_identifier_from_user_input_passes() -> None:
    """用户自己提到的编号不算幻觉（结果为空也只是如实告知）。"""
    result = detect_hallucinations(
        "未找到 SO000999 的订单", [_orders_execution(rows=[])], user_input="查 SO000999"
    )
    assert result.has_hallucination is False


def test_identifier_from_failed_execution_parameters_passes() -> None:
    """失败调用的参数是模型真实发起过的查询，引用它不算编造。"""
    result = detect_hallucinations(
        "查询 SO000999 失败", [_failed_execution(parameters={"bill_no": "SO000999"})]
    )
    assert result.has_hallucination is False


def test_identifier_without_any_execution_detected() -> None:
    result = detect_hallucinations("客户 CUS001 有两笔订单", [])
    assert result.has_hallucination is True
    assert result.detection_type == "entity fabrication"


# ── Check 3：业务一致性 ──


def test_status_mismatch_detected() -> None:
    """实体图记录 SO000008 已审核（C），回复声称暂存 → 业务不一致。"""
    result = detect_hallucinations(
        "订单 SO000008 是暂存状态", [_orders_execution()], [_order_entity("C")]
    )
    assert result.has_hallucination is True
    assert result.detection_type == "business logic error"
    item = result.hallucinated_items[0]
    assert item.claimed_value == "暂存"
    assert item.actual_value == "已审核"


def test_status_match_passes() -> None:
    assert (
        detect_hallucinations(
            "订单 SO000008 已审核", [_orders_execution()], [_order_entity("C")]
        ).has_hallucination
        is False
    )


def test_entity_without_status_attribute_skipped() -> None:
    entity = EntityRef(entity_type="customer", display_name="测试客户", kingdee_id="CUS001")
    result = detect_hallucinations("客户 CUS001 已审核", [_orders_execution()], [entity])
    assert result.has_hallucination is False


# ── 干净回复与结果结构 ──


def test_clean_response_has_no_hallucination() -> None:
    result = detect_hallucinations("查询完成，结果如上。", [_orders_execution()])
    assert isinstance(result, HallucinationDetectionResult)
    assert result.has_hallucination is False
    assert result.detection_type is None
    assert result.hallucinated_items == ()
    assert result.confidence == 0.0
    assert result.correction_hint == ""


def test_result_carries_reference_facts_and_hint() -> None:
    result = detect_hallucinations("订单 SO000999 已审核", [_orders_execution()])
    assert result.confidence > 0.0
    assert "SO000999" in result.correction_hint
    assert "SO000001" in result.reference_facts["identifiers"]
    assert result.reference_facts["counts"] == [2]


# ── 验收：注入式幻觉 fixture 检出率 100% ──


class _InjectedCase:
    """一条注入式幻觉 fixture：响应 + 事实源 → 必须命中。"""

    def __init__(
        self,
        name: str,
        response: str,
        executions: list[ToolExecution],
        entities: list[EntityRef] | None = None,
    ) -> None:
        self.name = name
        self.response = response
        self.executions = executions
        self.entities = entities or []


_INJECTED_CASES: list[_InjectedCase] = [
    _InjectedCase("数字编造（数量）", "查到 5 张订单", [_orders_execution()]),
    _InjectedCase("数字编造（金额单位）", "合计金额 30 万元", [_orders_execution()]),
    _InjectedCase("中文数量编造", "查到三条订单", [_orders_execution()]),
    _InjectedCase("订单号编造", "订单 SO000999 已审核", [_orders_execution()]),
    _InjectedCase("客户号编造", "客户 CUS999 的资料如下", [_orders_execution()]),
    _InjectedCase(
        "业务状态编造",
        "订单 SO000008 是暂存状态",
        [_orders_execution()],
        [_order_entity("C")],
    ),
    _InjectedCase("引用未执行过的结果", "订单 SO000008 已审核", []),
    _InjectedCase(
        "引用失败调用的结果",
        "订单 SO000008 金额为 45000",
        [_failed_execution()],
    ),
]


def test_injected_hallucination_fixtures_all_detected() -> None:
    """验收口径：注入的每条幻觉都必须被检出（检出率 100%）。"""
    detected = 0
    for case in _INJECTED_CASES:
        result = detect_hallucinations(case.response, case.executions, case.entities)
        assert result.has_hallucination, f"漏检: {case.name}"
        detected += 1
    assert detected / len(_INJECTED_CASES) == 1.0
