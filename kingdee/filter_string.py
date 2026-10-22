"""FilterExpression → FilterString 渲染器（金蝶适配器内部）+ 黑名单自检。

职责：
- 把中性的 ``FilterExpression`` 翻译为金蝶 K3Cloud 的 FilterString 方言
- 字面量格式化（``quote_eq`` / ``quote_like`` / ``fmt_number``）
- **黑名单自检**（T04 起从 ``safety/guard_sql.py`` 下沉）：
  ``_DANGEROUS_PATTERNS`` / ``_mask_quoted`` / ``_parse_literal`` /
  ``parse_filter_string`` 原本是 Guard 2 对「外部不可信 FilterString」的校验
  设施。T04 起 Guard 2 主路径改为校验 ``FilterExpression``（T06 落地），
  这套字符串设施下沉到本渲染器内部做**自检**——防御渲染器自身的 bug
  导致危险产物（如条件拼接错位的 OR 注入）。
- 渲染产出 FilterString 后，用 ``parse_filter_string`` 做 round-trip 自检：
  渲染产物必须能被受限文法解析回来，否则说明渲染器逻辑出现回归。

Why 按 ``type(value)`` 分派而非查端点 ``data_type``：
- ``FilterCondition.value`` 的类型约定由端点层保证（date 收 ``"YYYY-MM-DD"``
  字符串、number 收 float），渲染器只需按实际类型渲染
- 「value 类型与端点 data_type 匹配」的校验归 T06 Guard 2，渲染器不越权
- 避免渲染器反向依赖端点注册表（防循环导入）

Why ``ParsedCondition`` 与 ``erp.types.FilterCondition`` 名字不同：
``erp.types.FilterCondition`` 是结构化输入（Guard 2 的主校验对象），
``ParsedCondition`` 是 FilterString 字符串解析的产物（仅黑名单自检用）。
两者语义不同，名字必须区分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from erp.types import FilterCondition, FilterExpression, FilterOp

__all__ = [
    "FilterSyntaxError",
    "ParsedCondition",
    "fmt_number",
    "parse_filter_string",
    "quote_eq",
    "quote_like",
    "render_filter_expression",
]


def quote_eq(value: str) -> str:
    """等值条件的带引号字面量（``FDocumentStatus = 'C'``）。

    Why 拒绝而非转义单引号：金蝶 FilterString 的转义规则各版本不一
    （``''`` / ``\\'``），转义 = 把不确定的协议行为引入系统；拒绝把模糊性
    挡在边界外。
    """
    if "'" in value:
        raise ValueError(f"过滤值不能包含单引号: {value!r}")
    return f"'{value}'"


def quote_like(value: str) -> str:
    """模糊匹配条件的带引号字面量（``FName LIKE '%测试%'``）。单引号拒绝同上。"""
    if "'" in value:
        raise ValueError(f"过滤值不能包含单引号: {value!r}")
    return f"'%{value}%'"


def fmt_number(value: float) -> str:
    """数值条件的字面量：整数值去掉 ``.0`` 尾巴（金蝶 FilterString 数值比较惯例）。"""
    return str(int(value)) if value.is_integer() else repr(value)


def _render_literal(condition: FilterCondition) -> str:
    """按 value 类型渲染字面量（str → 引号，float → 数值）。

    LIKE 的 value 必须是 str（其他操作符不接受模糊通配符）。
    """
    value = condition.value
    if condition.op is FilterOp.LIKE:
        if not isinstance(value, str):
            raise ValueError(f"LIKE 条件的值必须是字符串: {value!r}")
        return quote_like(value)
    if isinstance(value, str):
        return quote_eq(value)
    return fmt_number(value)


_OPERATOR_RENDER: dict[FilterOp, str] = {
    FilterOp.EQ: "=",
    FilterOp.NE: "<>",
    FilterOp.GT: ">",
    FilterOp.GE: ">=",
    FilterOp.LT: "<",
    FilterOp.LE: "<=",
    FilterOp.LIKE: "LIKE",
}


# ── 黑名单与受限文法自检设施（T04 起从 safety/guard_sql.py 下沉）──
#
# 设计主张：
# - **不引入 sqlparse**：它不在方案 §1.3 技术选型内，且定位是格式化工具而非安全
#   校验器。这里手写「受限文法白名单」：只接受 AND 连接、六种比较符 + LIKE、
#   点号关联字段、单引号字符串或数值字面量——比通用 SQL 解析更小、更可审计。
# - **黑名单在引号掩码后匹配**：客户名里的 ``A OR B``、``UPDATE`` 是合法字面量，
#   不能被误杀；注入要生效必须「逃出引号」，掩码后仍能被关键字/注释/分号规则捕获。

# 比较符按「长符号在前」排列，否则 >= 会被 > 抢先匹配成「大于 + '=xxx'」
_CONDITION_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    r"\s*(?P<op><>|>=|<=|=|>|<|(?i:LIKE))\s*(?P<value>.+?)\s*$"
)
_AND_RE = re.compile(r"\s+AND\s+", re.IGNORECASE)
_OR_NOT_RE = re.compile(r"\b(?:OR|NOT)\b", re.IGNORECASE)
_NUMERIC_LITERAL_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# 危险模式：标签 + 正则（在掩码串上匹配，引号内字面量不参与）
_DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "危险 SQL 关键字",
        re.compile(
            r"\b(?:DROP|UPDATE|DELETE|INSERT|ALTER|TRUNCATE|EXEC|EXECUTE|CREATE|MERGE)\b",
            re.IGNORECASE,
        ),
    ),
    ("UNION 注入", re.compile(r"\bUNION\b", re.IGNORECASE)),
    ("时间盲注函数", re.compile(r"\b(?:SLEEP|BENCHMARK|WAITFOR|PG_SLEEP)\b", re.IGNORECASE)),
    ("SQL 注释", re.compile(r"--|/\*|\*/|#")),
    ("语句分隔符", re.compile(r";")),
    ("恒真条件", re.compile(r"\b1\s*=\s*1\b")),
    ("转义字符", re.compile(r"\\")),
)


class FilterSyntaxError(ValueError):
    """FilterString 超出受限文法或命中危险模式（fail-closed 的上报载体）。"""


@dataclass(frozen=True)
class ParsedCondition:
    """FilterString 字符串解析的产物（``value`` 已去除外层单引号）。

    与 ``erp.types.FilterCondition`` 的语义区别：本类型仅承载字符串解析结果
    （``operator`` 是渲染后的符号如 ``">="`` / ``"LIKE"``，``value`` 是去引号
    字符串），黑名单自检用；结构化输入校验归 Guard 2 走 ``erp.types.FilterCondition``。
    """

    field: str
    operator: str
    value: str


def _mask_quoted(text: str) -> str:
    """把单引号内的字符替换为等长 ``~``，保留引号本身，索引与原串 1:1 对齐。

    Why：引号内的 OR / UPDATE / 括号是**字面量**，不能当语法符号；掩码后
    在黑名单与语法检测中天然被忽略，同时不会改变原串的切片位置。
    """
    chars = list(text)
    in_quote = False
    for index, char in enumerate(chars):
        if char == "'":
            in_quote = not in_quote
            continue
        if in_quote:
            chars[index] = "~"
    return "".join(chars)


def _check_dangerous_patterns(masked: str) -> None:
    """在掩码串上做危险模式黑名单匹配。"""
    for label, pattern in _DANGEROUS_PATTERNS:
        matched = pattern.search(masked)
        if matched is not None:
            raise FilterSyntaxError(f"检测到危险模式（{label}）: {matched.group(0)}")


def _parse_literal(raw: str) -> str:
    """校验并归一化字面量：只允许成对单引号字符串或数值。"""
    value = raw.strip()
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        inner = value[1:-1]
        if "'" in inner:
            raise FilterSyntaxError("字符串字面量内不允许单引号")
        return inner
    if _NUMERIC_LITERAL_RE.match(value):
        return value
    raise FilterSyntaxError(f"非法字面量: {value!r}（只允许单引号字符串或数值）")


def parse_filter_string(filter_string: str) -> tuple[ParsedCondition, ...]:
    """把 FilterString 解析成条件元组；空串返回空元组。

    Raises:
        FilterSyntaxError: 命中危险模式、含 OR/NOT/括号、引号未闭合、
            或任一条件不符合受限文法。
    """
    text = filter_string.strip()
    if not text:
        return ()

    masked = _mask_quoted(text)
    if masked.count("'") % 2 != 0:
        raise FilterSyntaxError("单引号未闭合")
    # 危险模式优先判定：让 SLEEP(...) 报「时间盲注」而非笼统的「括号不支持」
    _check_dangerous_patterns(masked)
    if "(" in masked or ")" in masked:
        raise FilterSyntaxError("不支持括号分组")
    if _OR_NOT_RE.search(masked):
        raise FilterSyntaxError("不支持 OR / NOT 条件")

    # 在掩码串上定位 " AND " 切分点，再回原串按位置切片（引号内 AND 已掩蔽）
    segments: list[str] = []
    cursor = 0
    for match in _AND_RE.finditer(masked):
        segments.append(text[cursor : match.start()])
        cursor = match.end()
    segments.append(text[cursor:])

    conditions: list[ParsedCondition] = []
    for segment in segments:
        matched = _CONDITION_RE.match(segment)
        if matched is None:
            raise FilterSyntaxError(f"无法解析的过滤条件: {segment!r}")
        conditions.append(
            ParsedCondition(
                field=matched.group("field"),
                operator=matched.group("op").upper(),
                value=_parse_literal(matched.group("value")),
            )
        )
    return tuple(conditions)


def render_filter_expression(expr: FilterExpression) -> str:
    """FilterExpression → FilterString；空表达式返回空串。

    渲染规则：
    - 每个条件渲染为 ``field <op> <literal>``
    - 多条件用 `` AND `` 连接（与既有 ``build_*_filter`` 的拼接语义一致）

    自检（T04 起）：渲染产物必须能被 ``parse_filter_string`` 解析回来——
    防御渲染器自身的 bug（如条件拼接错位导致 OR 注入面）。自检失败抛
    ``FilterSyntaxError``，调用方按 fail-closed 处理。
    """
    segments: list[str] = []
    for condition in expr.conditions:
        operator = _OPERATOR_RENDER[condition.op]
        literal = _render_literal(condition)
        segments.append(f"{condition.field} {operator} {literal}")
    rendered = " AND ".join(segments)
    # round-trip 自检：渲染产物必须落回受限文法
    parse_filter_string(rendered)
    return rendered
