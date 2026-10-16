"""仿真沙箱的 FilterString 受限解析（M1-T06a）。

定位：**仿真端的过滤语法守卫**，与 M4 Guard 2（AST 白名单校验）职责不同——
两者都「拒绝」，但拒绝的理由不同：Guard 2 拒绝的是**危险模式**（注入类），
本解析器拒绝的是**仿真端未实现的语法**（如 OR 分组）。仿真端宁可明确报错，
也绝不静默忽略不支持的语法——静默忽略会让被测代码在仿真端「测试通过」，
换到真环境却因语法差异失败（假绿，契约测试最忌讳的错误）。

支持范围（够用即止，保持实现可审阅）：
- 多条件 ``AND`` 连接（金蝶查询的绝大多数用法）
- 比较符 ``=`` ``<>`` ``>`` ``>=`` ``<`` ``<=``
- 模式匹配 ``LIKE``（``%`` 任意串、``_`` 单字符，SQL 通配语义）

不支持（显式报错，fail-closed）：``OR`` / 括号分组 / ``NOT``。

Why 用「掩码 + 切片」而非直接正则整串：引号内的 ``OR``、``AND``、括号是**字面量**，
不能当语法符号。先把单引号内字符替换成等长的 ``~`` 得到掩码串（索引与原串 1:1 对齐），
在掩码串上找语法符号的位置，再回原串按位置切片——避免引号内容污染语法判定。
"""

import re
from dataclasses import dataclass

# 单个条件：字段名 [比较符] 字面量
# 字段名允许点号关联形式（FCustomerID.FNumber）；比较符按「长符号在前」排列，
# 否则 ``>=`` 会被 ``>`` 抢先匹配成「大于 + '=xxx'」。
_CONDITION_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    r"\s*(?P<op><>|>=|<=|=|>|<|(?i:LIKE))\s*(?P<value>.+?)\s*$"
)
# 掩码串上检测「独立词」的 OR / NOT（\b 保证不会误伤 FSaleOrgId 这类含 or 的字段名）
_OR_NOT_RE = re.compile(r"\b(?:OR|NOT)\b", re.IGNORECASE)
# 掩码串上定位 AND 切分点
_AND_RE = re.compile(r"\s+AND\s+", re.IGNORECASE)

_SUPPORTED_OPS = frozenset({"=", "<>", ">", ">=", "<", "<=", "LIKE"})


@dataclass(frozen=True)
class Condition:
    """解析后的单个过滤条件（``value`` 已去除外层单引号）。"""

    field: str
    op: str
    value: str


class FilterSyntaxError(ValueError):
    """FilterString 超出了仿真端支持的语法范围（不静默忽略，直接报错）。"""


def _mask_quoted(text: str) -> str:
    """把单引号内的字符替换为等长 ``~``，保留引号本身，索引与原串 1:1 对齐。"""
    chars = list(text)
    in_quote = False
    for index, char in enumerate(chars):
        if char == "'":
            in_quote = not in_quote
            continue
        if in_quote:
            chars[index] = "~"
    return "".join(chars)


def _unquote(value: str) -> str:
    """去掉字面量外层成对的单引号（仅一层，不做转义解释）。"""
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def parse_filter(filter_string: str) -> tuple[Condition, ...]:
    """把 FilterString 解析成条件元组；空串返回空元组。

    Args:
        filter_string: 金蝶 ``FilterString`` 原文，如 ``"FDate >= '2026-01-01' AND FQty > 5"``。

    Returns:
        条件元组（``AND`` 连接的顺序保留）。

    Raises:
        FilterSyntaxError: 含 ``OR`` / 括号 / ``NOT``、引号未闭合、或单个条件格式非法。
    """
    text = filter_string.strip()
    if not text:
        return ()

    masked = _mask_quoted(text)
    if masked.count("'") % 2 != 0:
        raise FilterSyntaxError(f"单引号未闭合: {filter_string!r}")
    if "(" in masked or ")" in masked:
        raise FilterSyntaxError(f"仿真端不支持括号分组: {filter_string!r}")
    if _OR_NOT_RE.search(masked):
        raise FilterSyntaxError(f"仿真端不支持 OR / NOT: {filter_string!r}")

    # 在掩码串上定位所有 " AND " 切分点，再回原串按位置切片（引号内的 AND 已掩蔽）
    segments: list[str] = []
    cursor = 0
    for match in _AND_RE.finditer(masked):
        segments.append(text[cursor : match.start()])
        cursor = match.end()
    segments.append(text[cursor:])

    conditions: list[Condition] = []
    for segment in segments:
        matched = _CONDITION_RE.match(segment)
        if matched is None:
            raise FilterSyntaxError(f"无法解析的过滤条件: {segment!r}")
        op = matched.group("op").upper()
        if op not in _SUPPORTED_OPS:
            raise FilterSyntaxError(f"不支持的比较符 {op!r}（条件: {segment!r}）")
        conditions.append(
            Condition(
                field=matched.group("field"),
                op=op,
                value=_unquote(matched.group("value")),
            )
        )
    return tuple(conditions)
