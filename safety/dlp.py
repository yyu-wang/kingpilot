"""DLP 出域扫描（M7-T02，方案 §5.4 支柱十一）。

最终答复发送前的最后一道检查：命中手机号 / 身份证 / 银行卡 / 邮箱即阻断。
与 M7-T01 的分工：
- T01 是「掩码后放行」，处理工具结果与日志出口；
- T02 是「发现即阻断」，只报类别与数量，绝不记录命中原值。

正则来自 ``safety.datasecurity.PII_PATTERNS``，保证两处口径同源。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from safety.datasecurity import PII_PATTERNS


@dataclass(frozen=True, slots=True)
class DlpFinding:
    """一类敏感信息的命中统计（不含原值）。"""

    category: str
    count: int


@dataclass(frozen=True, slots=True)
class DlpScanResult:
    """一次出域扫描的结果。"""

    findings: tuple[DlpFinding, ...] = ()

    @property
    def blocked(self) -> bool:
        """任意命中即阻断（fail-closed）。"""
        return bool(self.findings)

    @property
    def categories(self) -> tuple[str, ...]:
        """命中的类别列表（审计/告警用，不含敏感值）。"""
        return tuple(finding.category for finding in self.findings)

    @property
    def total(self) -> int:
        """命中总数。"""
        return sum(finding.count for finding in self.findings)


class DlpScanner:
    """基于正则的出域扫描器。

    扫描顺序固定为身份证 → 手机 → 银行卡 → 邮箱，并记录已覆盖跨度：
    18 位身份证同时满足银行卡 ``\\d{16,19}``，先去重可避免同一串数字
    被报成两类，导致审计统计失真。
    """

    def __init__(
        self,
        patterns: Sequence[tuple[str, re.Pattern[str]]] = PII_PATTERNS,
    ) -> None:
        self._patterns = tuple(patterns)

    def scan(self, text: str) -> DlpScanResult:
        """扫描自由文本；空文本直接放行。"""
        if not text:
            return DlpScanResult()

        covered: list[tuple[int, int]] = []
        findings: list[DlpFinding] = []
        for category, pattern in self._patterns:
            count = 0
            for match in pattern.finditer(text):
                start, end = match.span()
                if _overlaps(start, end, covered):
                    continue
                covered.append((start, end))
                count += 1
            if count:
                findings.append(DlpFinding(category=category, count=count))
        return DlpScanResult(findings=tuple(findings))


def _overlaps(start: int, end: int, covered: Sequence[tuple[int, int]]) -> bool:
    """判断区间是否与已统计的命中重叠。"""
    return any(
        start < covered_end and covered_start < end for covered_start, covered_end in covered
    )
