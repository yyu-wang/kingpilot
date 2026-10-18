"""Guard 2（M4-T03）：FilterString 危险模式黑名单 + 受限语法解析 + 字段白名单。

方案 §5.1 五层 Guard 的第二层。校验对象是**组装后的 FilterString**（而不是参数）：
工具层先用参数模型组装 FilterString，本 Guard 再对产物做纵深校验——即使未来某个
``build_filter`` 写错或被污染，也能在发给金蝶之前拦住。

实现决策：
- **不引入 sqlparse**：它不在方案 §1.3 技术选型内，且定位是格式化工具而非安全
  校验器。这里手写「受限文法白名单」：只接受 AND 连接、六种比较符 + LIKE、
  点号关联字段、单引号字符串或数值字面量——比通用 SQL 解析更小、更可审计。
- **黑名单在引号掩码后匹配**：客户名里的 ``A OR B``、``UPDATE`` 是合法字面量，
  不能被误杀；注入要生效必须「逃出引号」，掩码后仍能被关键字/注释/分号规则捕获。
- **字段白名单**取端点的 ``filterable_fields``（可查 ≠ 可过滤，单一真相源在
  ``kingdee/endpoints/``）。
- **空 FilterString 放行**：无条件查询拒绝归 T05，避免同一规则两处实现。
- 参数非法 / 组装层拒绝 → fail-closed 拦截；其它异常由 Guard 管道统一兜底。
"""

import re
from dataclasses import dataclass

from pydantic import ValidationError

from agent.tools import ToolRegistry
from infrastructure.exceptions import ToolError
from safety.guard import GuardContext, GuardResult

GUARD_NAME = "guard_filter"

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
class FilterCondition:
    """解析后的单个过滤条件（``value`` 已去除外层单引号）。"""

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


def parse_filter_string(filter_string: str) -> tuple[FilterCondition, ...]:
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

    conditions: list[FilterCondition] = []
    for segment in segments:
        matched = _CONDITION_RE.match(segment)
        if matched is None:
            raise FilterSyntaxError(f"无法解析的过滤条件: {segment!r}")
        conditions.append(
            FilterCondition(
                field=matched.group("field"),
                operator=matched.group("op").upper(),
                value=_parse_literal(matched.group("value")),
            )
        )
    return tuple(conditions)


class FilterGuard:
    """Guard 2：对工具组装出的 FilterString 做危险模式、语法与字段白名单校验。"""

    name = GUARD_NAME

    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    async def check(self, context: GuardContext) -> GuardResult:
        """按「组装 → 黑名单/语法 → 字段白名单」顺序校验。"""
        try:
            tool = self._tools.get(context.tool_name)
        except ToolError:
            return GuardResult.block(
                f"工具 {context.tool_name} 未注册，无法校验过滤条件",
                detail={"tool": context.tool_name},
            )

        try:
            params = tool.params_model.model_validate(dict(context.arguments))
        except ValidationError as exc:
            return GuardResult.block(
                f"参数校验失败，无法校验过滤条件: {context.tool_name}",
                detail={"tool": context.tool_name, "error_count": len(exc.errors())},
            )

        try:
            filter_string = tool.build_filter(params)
        except ValueError as exc:
            # 组装层拒绝（如过滤值含单引号）——归一为拦截，不把原文回显进原因
            return GuardResult.block(
                f"过滤条件组装失败: {context.tool_name}（{type(exc).__name__}）",
                detail={"tool": context.tool_name, "error_type": type(exc).__name__},
            )

        if not filter_string.strip():
            # 无条件查询的拒绝归 T05：本 Guard 只负责「有条件的查询是否安全」
            return GuardResult.allow(detail={"tool": context.tool_name, "filter_empty": True})

        try:
            conditions = parse_filter_string(filter_string)
        except FilterSyntaxError as exc:
            return GuardResult.block(
                f"过滤条件非法: {exc}",
                detail={"tool": context.tool_name, "filter_string": filter_string},
            )

        allowed = tool.endpoint.filterable_fields
        for condition in conditions:
            if condition.field not in allowed:
                return GuardResult.block(
                    f"字段 {condition.field} 不在 {tool.endpoint.form_id} 可过滤字段白名单内",
                    detail={
                        "tool": context.tool_name,
                        "field": condition.field,
                        "allowed_fields": sorted(allowed),
                    },
                )

        return GuardResult.allow(
            detail={
                "tool": context.tool_name,
                "condition_count": len(conditions),
                "fields": [condition.field for condition in conditions],
            }
        )
