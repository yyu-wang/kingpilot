"""幻觉检测器（M3-T06，方案 §5.3 支柱十「结果阶段三重校验」）。

检测口径（确定性规则，不调 LLM——可单测、零 token、可进默认门禁）：

1. **数字事实核对**：回复里的数字必须能在工具实际返回值中找到。
   支持 ``25.6万 → 256000`` 的单位换算；中文数量词（两条/3张/2家…）
   与工具返回行数比对。日期/百分比/序数不纳入业务事实核对（避免误报，
   口径见 ``_extract_numeric_claims``）。
2. **Tool 结果来源**：回复引用的业务标识（SO000008/CUS001/MAT001…）
   必须来自「成功执行结果 ∪ 实体图 ∪ 用户输入 ∪ 实际调用参数」；
   无任何成功执行却引用标识同样判编造。
3. **业务一致性**：实体图中订单的 ``FDocumentStatus`` 与回复声称的状态词
   （暂存/创建/审核中/已审核/重新审核）在同句内不一致即命中。

边界：本任务只做「检测」；自动纠错循环与降级回复归 M4（安全管道就绪后），
跨句指代消解与金额汇总核对归 M7/M8 评测体系。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from models.memory import EntityRef
from models.tool import ToolExecution

DetectionType = Literal[
    "number mismatch",
    "tool result hallucination",
    "entity fabrication",
    "business logic error",
]
Severity = Literal["low", "medium", "high", "critical"]

# ── 抽取规则 ──

# 业务标识：字母前缀 + 数字（金蝶编码常见形态），前后不能粘字母数字
_IDENTIFIER_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,}\d{3,})(?![A-Za-z0-9])")
# 阿拉伯数字（排除紧邻字母数字的片段，避免把 SO000008 拆成数字）
_ARABIC_PATTERN = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)(?![A-Za-z0-9])")
# 数量词：数字 + 计数单位（用于和工具返回行数比对）
_COUNT_UNITS = frozenset({"张", "笔", "条", "家", "个", "位", "款", "项", "份"})
# 不参与业务事实核对的单位：日期/时间/百分比（含全角）
_SKIP_UNITS = frozenset({"年", "月", "日", "号", "时", "分", "秒", "%", "％"})
# 数字后的量级（万/亿），元等货币单位不参与解析
_MAGNITUDES: dict[str, Decimal] = {"万": Decimal(10000), "亿": Decimal(100000000)}
# 捕获数字后的可选量级与可选单位
_ARABIC_CLAIM = re.compile(
    r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(万|亿)?\s*"
    r"(%|％|年|月|日|号|时|分|秒|张|笔|条|家|个|位|款|项|份)?"
)
_CHINESE_COUNT = re.compile(r"([零一二两三四五六七八九十百千]+)\s*([张笔条家个位款项份])")
# 日期片段（2026-03-05）不拆成事实数字：否则日号 3 会掩盖「三条」这类数量编造
_DATE_LIKE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

# 业务状态词 → FDocumentStatus 码（长词优先，避免「已审核」被「审核中」误匹配）
_STATUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("重新审核", "D"),
    ("审核中", "B"),
    ("已审核", "C"),
    ("暂存", "Z"),
    ("创建", "A"),
)
_STATUS_LABELS = {"Z": "暂存", "A": "创建", "B": "审核中", "C": "已审核", "D": "重新审核"}

_SEVERITY_CONFIDENCE: dict[Severity, float] = {
    "critical": 0.99,
    "high": 0.95,
    "medium": 0.8,
    "low": 0.6,
}


@dataclass(frozen=True)
class HallucinatedItem:
    """单条幻觉明细（对齐方案 §支柱十 ``HallucinatedItem``）。"""

    text: str
    claimed_value: Any
    actual_value: Any = None
    severity: Severity = "high"
    detection_type: DetectionType = "number mismatch"


@dataclass(frozen=True)
class HallucinationDetectionResult:
    """检测结果（对齐方案 §支柱十 ``HallucinationDetectionResult``）。

    ``correction_hint`` 供 M4 纠错循环构造 Prompt；``reference_facts``
    保留本次比对用的事实底账，便于审计复现。
    """

    has_hallucination: bool
    detection_type: DetectionType | None
    hallucinated_items: tuple[HallucinatedItem, ...]
    confidence: float
    correction_hint: str
    reference_facts: dict[str, Any]


def detect_hallucinations(
    response: str,
    tool_executions: Sequence[ToolExecution],
    entities: Sequence[EntityRef] = (),
    *,
    user_input: str = "",
) -> HallucinationDetectionResult:
    """三重校验入口：数字事实 / 结果来源 / 业务一致性。

    Args:
        response: LLM 最终答复。
        tool_executions: 全链路工具执行记录（只采信成功且 result 非空的）。
        entities: 实体图（Layer 3，业务一致性校验用）。
        user_input: 用户原始输入（用户自己提到的标识不算幻觉）。

    Returns:
        命中即 ``has_hallucination=True``；``detection_type`` 取最高危条目类型。
    """
    numbers, count_values, identifiers = _collect_facts(tool_executions, entities, user_input)
    items: list[HallucinatedItem] = []

    items.extend(_check_numbers(response, numbers, count_values))
    items.extend(_check_identifiers(response, identifiers))
    items.extend(_check_business_consistency(response, entities))

    if not items:
        return HallucinationDetectionResult(
            has_hallucination=False,
            detection_type=None,
            hallucinated_items=(),
            confidence=0.0,
            correction_hint="",
            reference_facts=_reference_facts_payload(numbers, count_values, identifiers),
        )

    primary = max(items, key=lambda item: _SEVERITY_CONFIDENCE[item.severity])
    return HallucinationDetectionResult(
        has_hallucination=True,
        detection_type=primary.detection_type,
        hallucinated_items=tuple(items),
        confidence=_SEVERITY_CONFIDENCE[primary.severity],
        correction_hint=_build_hint(items),
        reference_facts=_reference_facts_payload(numbers, count_values, identifiers),
    )


# ── 事实源 ──


def _collect_facts(
    tool_executions: Sequence[ToolExecution],
    entities: Sequence[EntityRef],
    user_input: str,
) -> tuple[frozenset[Decimal], frozenset[int], frozenset[str]]:
    """汇总可信事实：数字、行数、业务标识（白名单）。"""
    numbers: set[Decimal] = set()
    counts: set[int] = set()
    identifiers: set[str] = set()

    for execution in tool_executions:
        # 实际调用参数：模型引用「刚查过的编号」不算编造（结果可能为空）
        _absorb_values(execution.parameters, numbers, identifiers)
        if execution.error is not None or execution.result is None:
            continue
        _absorb_values(execution.result, numbers, identifiers)
        row_count = _row_count(execution.result)
        if row_count is not None:
            counts.add(row_count)
    if counts:
        counts.add(sum(counts))  # 多次执行的合计行数也是合法口径

    for entity in entities:
        if entity.kingdee_id:
            identifiers.add(entity.kingdee_id.upper())
        if entity.display_name:
            identifiers.add(entity.display_name.upper())
        _absorb_values(entity.attributes, numbers, identifiers)

    identifiers.update(match.upper() for match in _IDENTIFIER_PATTERN.findall(user_input))
    _absorb_values(user_input, numbers, identifiers)

    return frozenset(numbers), frozenset(counts), frozenset(identifiers)


def _absorb_values(value: object, numbers: set[Decimal], identifiers: set[str]) -> None:
    """递归吸收任意 JSON 形状里的数字与标识（dict/list/str/数值）。"""
    if isinstance(value, dict):
        for item in value.values():
            _absorb_values(item, numbers, identifiers)
        return
    if isinstance(value, list | tuple | set):
        for item in value:
            _absorb_values(item, numbers, identifiers)
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float | Decimal):
        _add_number(numbers, value)
        return
    text = _DATE_LIKE.sub(" ", str(value))
    for raw in _ARABIC_PATTERN.findall(text):
        _add_number(numbers, raw)
    identifiers.update(match.upper() for match in _IDENTIFIER_PATTERN.findall(text))


def _add_number(numbers: set[Decimal], value: object) -> None:
    """数值归一化后入集合（Decimal 数值相等即去重，忽略尾零）。"""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return
    if number.is_finite():
        numbers.add(number)


def _row_count(result: object) -> int | None:
    """工具结果的返回行数（``BillQueryResult`` 形状的 ``rows``）。"""
    if isinstance(result, dict):
        rows = result.get("rows")
        if isinstance(rows, list):
            return len(rows)
    return None


# ── Check 1：数字事实核对 ──


def _check_numbers(
    response: str, numbers: frozenset[Decimal], counts: frozenset[int]
) -> list[HallucinatedItem]:
    items: list[HallucinatedItem] = []
    date_spans = [match.span() for match in _DATE_LIKE.finditer(response)]
    for match in _ARABIC_CLAIM.finditer(response):
        raw, magnitude, unit = match.group(1), match.group(2), match.group(3)
        if unit in _SKIP_UNITS:
            continue  # 日期/时间/百分比不属业务事实
        if _is_ordinal(response, match.start()) or _in_spans(
            match.start(), match.end(), date_spans
        ):
            continue
        claimed = _to_decimal(raw)
        if claimed is None:
            continue
        if magnitude:
            claimed *= _MAGNITUDES[magnitude]
        if unit in _COUNT_UNITS:
            if int(claimed) in counts:
                continue
            items.append(
                HallucinatedItem(
                    text=match.group(0),
                    claimed_value=int(claimed),
                    actual_value=sorted(counts),
                    severity="high",
                    detection_type="tool result hallucination",
                )
            )
            continue
        if claimed in numbers or claimed in counts:
            continue
        items.append(
            HallucinatedItem(
                text=match.group(0),
                claimed_value=_format_number(claimed),
                actual_value=None,
                severity="high",
                detection_type="number mismatch",
            )
        )

    for match in _CHINESE_COUNT.finditer(response):
        if _is_ordinal(response, match.start()):
            continue
        chinese_claimed = _parse_chinese_number(match.group(1))
        # 数量词只与工具返回行数比对（不与任意数字比，避免被日期等碎片掩盖）
        if chinese_claimed is None or chinese_claimed in counts:
            continue
        items.append(
            HallucinatedItem(
                text=match.group(0),
                claimed_value=chinese_claimed,
                actual_value=sorted(counts),
                severity="high",
                detection_type="tool result hallucination",
            )
        )
    return items


def _is_ordinal(text: str, start: int) -> bool:
    """序数（第 N）不是事实断言，跳过。"""
    return start > 0 and text[start - 1] == "第"


def _in_spans(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    """匹配区间是否落在日期片段内（ISO 日期里的年月日不是业务数字）。"""
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_chinese_number(text: str) -> int | None:
    """解析百千以内的中文数字（十五/二十三/一百零五/两千）。"""
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            total += (current or 1) * _CN_UNITS[char]
            current = 0
        else:
            return None
    return total + current


def _format_number(value: Decimal) -> str:
    """输出人类可读数值（去尾零，不用科学计数法）。"""
    normalized = value.normalize()
    return format(normalized, "f")


# ── Check 2：Tool 结果来源（标识编造） ──


def _check_identifiers(response: str, identifiers: frozenset[str]) -> list[HallucinatedItem]:
    items: list[HallucinatedItem] = []
    seen: set[str] = set()
    for match in _IDENTIFIER_PATTERN.finditer(response):
        token = match.group(1).upper()
        if token in seen or token in identifiers:
            continue
        seen.add(token)
        items.append(
            HallucinatedItem(
                text=match.group(0),
                claimed_value=token,
                actual_value=None,
                severity="critical",
                detection_type="entity fabrication",
            )
        )
    return items


# ── Check 3：业务一致性 ──


def _check_business_consistency(
    response: str, entities: Sequence[EntityRef]
) -> list[HallucinatedItem]:
    items: list[HallucinatedItem] = []
    for sentence in re.split(r"[。！？；\n]", response):
        if not sentence.strip():
            continue
        for entity in entities:
            actual_code = str(entity.attributes.get("FDocumentStatus") or "").upper()
            if actual_code not in _STATUS_LABELS:
                continue
            if not _mentions_entity(sentence, entity):
                continue
            for keyword, claimed_code in _STATUS_KEYWORDS:
                if keyword not in sentence or claimed_code == actual_code:
                    continue
                items.append(
                    HallucinatedItem(
                        text=sentence.strip(),
                        claimed_value=keyword,
                        actual_value=_STATUS_LABELS[actual_code],
                        severity="high",
                        detection_type="business logic error",
                    )
                )
                break
    return items


def _mentions_entity(sentence: str, entity: EntityRef) -> bool:
    if entity.kingdee_id and entity.kingdee_id.upper() in sentence.upper():
        return True
    return bool(entity.display_name and entity.display_name in sentence)


# ── 结果组装 ──


def _build_hint(items: Sequence[HallucinatedItem]) -> str:
    hints: list[str] = []
    for item in items[:5]:
        if item.detection_type == "entity fabrication":
            hints.append(f"标识 {item.claimed_value} 未出现在工具结果或用户输入中")
        elif item.detection_type == "business logic error":
            hints.append(f"「{item.text}」与实际状态不符，应为「{item.actual_value}」")
        else:
            actual = f"，实际为 {item.actual_value}" if item.actual_value is not None else ""
            hints.append(f"数字 {item.claimed_value} 不在工具返回结果中{actual}")
    return "；".join(hints)


def _reference_facts_payload(
    numbers: frozenset[Decimal],
    counts: frozenset[int],
    identifiers: frozenset[str],
) -> dict[str, Any]:
    return {
        "numbers": sorted(_format_number(number) for number in numbers),
        "counts": sorted(counts),
        "identifiers": sorted(identifiers),
        "number_count": len(numbers),
        "identifier_count": len(identifiers),
    }
