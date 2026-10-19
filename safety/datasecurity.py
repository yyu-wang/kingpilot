"""数据分级脱敏（M7-T01，方案 §5.4 支柱十一）。

统一出口：
1. 工具结果 → ``redact_rows``（在进入 Trace / LLM / 记忆之前完成）
2. 日志出口 → ``redact_text``（loguru patcher 逐条掩码）

分级与策略来自 ``config/data_security.py``；本模块只负责算法。
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from config.data_security import (
    FIELD_CLASSIFICATION,
    LEVEL_STRATEGY,
    DataLevel,
    MaskStrategy,
)

REDACTED = "[REDACTED]"

# 数字边界排除前后的小数点：避免把高精度浮点（如耗时 0.03779199323616922）
# 误判成银行卡/身份证；真实号码不会紧邻小数点。
_PHONE_RE = re.compile(r"(?<![\d.])(1[3-9]\d{9})(?![\d.])")
_ID_CARD_RE = re.compile(r"(?<![\d.])(\d{17}[\dXx])(?![\d.])")
_BANK_CARD_RE = re.compile(r"(?<![\d.])(\d{16,19})(?![\d.])")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")

# DLP 出域扫描（M7-T02）复用同一组正则，避免两套规则漂移。
# 顺序有意义：身份证（18 位数字）先于银行卡（16~19 位）扫描并做跨度去重。
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("id_card", _ID_CARD_RE),
    ("phone", _PHONE_RE),
    ("bank_card", _BANK_CARD_RE),
    ("email", _EMAIL_RE),
)


def classify_field(field_name: str) -> DataLevel:
    """字段分级：先精确匹配，再按关联字段末段匹配，默认 internal。"""
    if field_name in FIELD_CLASSIFICATION:
        return FIELD_CLASSIFICATION[field_name]
    leaf = field_name.rsplit(".", 1)[-1]
    return FIELD_CLASSIFICATION.get(leaf, "internal")


def mask_value(value: Any, strategy: MaskStrategy, *, field_name: str = "") -> Any:
    """按策略脱敏单个值（算法库入口，供测试与自定义策略复用）。"""
    if value is None:
        return None
    if strategy == "none":
        return value
    text = str(value)
    if strategy == "drop":
        return REDACTED
    if strategy == "hash":
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return _partial_mask(field_name, text)


def redact_value(field_name: str, value: Any) -> Any:
    """按字段分级与策略脱敏。"""
    level = classify_field(field_name)
    return mask_value(value, LEVEL_STRATEGY[level], field_name=field_name)


def redact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """递归脱敏一行（支持嵌套 dict / list[dict]）。"""
    result: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Mapping):
            result[key] = redact_row(value)
        elif isinstance(value, list):
            result[key] = [
                redact_row(item) if isinstance(item, Mapping) else redact_value(key, item)
                for item in value
            ]
        else:
            result[key] = redact_value(key, value)
    return result


def redact_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """批量脱敏查询结果行（返回新列表，不修改入参）。"""
    return [redact_row(row) for row in rows]


def redact_text(text: str) -> str:
    """自由文本 PII 掩码（日志出口 / 兜底展示）。"""
    if not text:
        return text
    text = _EMAIL_RE.sub(_mask_email, text)
    text = _ID_CARD_RE.sub(_mask_id_card, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return _BANK_CARD_RE.sub(_mask_bank_card, text)


def _partial_mask(field_name: str, text: str) -> str:
    leaf = field_name.rsplit(".", 1)[-1].lower()
    if ("phone" in leaf or "mobile" in leaf) and len(text) == 11:
        return _mask_phone_text(text)
    if "email" in leaf:
        matched = _EMAIL_RE.fullmatch(text)
        if matched is not None:
            return _mask_email(matched)
    if "idcard" in leaf and len(text) >= 8:
        return _mask_id_card_text(text)
    if "bank" in leaf and len(text) >= 4:
        return _mask_bank_card_text(text)
    return _generic_mask(text)


def _generic_mask(text: str) -> str:
    if not text:
        return ""
    if len(text) == 1:
        return "*"
    if len(text) == 2:
        return f"{text[0]}*"
    return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"


def _mask_email(match: re.Match[str]) -> str:
    local, domain = match.group(1), match.group(2)
    if len(local) <= 1:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def _mask_id_card(match: re.Match[str]) -> str:
    return _mask_id_card_text(match.group(1))


def _mask_phone(match: re.Match[str]) -> str:
    return _mask_phone_text(match.group(1))


def _mask_bank_card(match: re.Match[str]) -> str:
    return _mask_bank_card_text(match.group(1))


def _mask_phone_text(text: str) -> str:
    if len(text) != 11:
        return _generic_mask(text)
    return f"{text[:3]}****{text[-4:]}"


def _mask_id_card_text(text: str) -> str:
    if len(text) < 8:
        return _generic_mask(text)
    return f"{text[:6]}{'*' * (len(text) - 10)}{text[-4:]}"


def _mask_bank_card_text(text: str) -> str:
    if len(text) < 4:
        return _generic_mask(text)
    return f"**** **** **** {text[-4:]}"
