"""链式 hash（M5-T04，方案 §5.5 防篡改）。

每条账本记录含 ``prev_record_hash``（前条 hash）与 ``record_hash``（本条内容 hash）；
读取时从创世 hash 起逐条重算，任何内容/顺序/删除改动都会在某一条断链。

设计要点：
- **canonical JSON**：键排序、紧凑分隔符、``default=str``，同内容永远同 hash
- hash 输入 = ``prev_hash + ":" + canonical_json(payload)``；
  payload 中的 ``record_hash`` / ``prev_record_hash`` 字段被排除（避免自引用）
- 校验失败写 CRITICAL 日志（M6 接告警），不抛异常——账本读取方据结果决定阻断
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

#: 创世 hash（空链的前序值）
GENESIS_HASH = "0" * 64

#: 参与 hash 计算时需排除的字段（hash 自身/前序 hash 由参数表达）
_HASH_FIELDS = frozenset({"record_hash", "prev_record_hash"})


@dataclass(frozen=True)
class ChainVerification:
    """hash 链校验结果（可审计）。"""

    valid: bool
    checked: int
    broken_index: int | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "valid": self.valid,
            "checked": self.checked,
            "broken_index": self.broken_index,
            "reason": self.reason,
        }


def compute_record_hash(payload: Mapping[str, Any], previous_hash: str = GENESIS_HASH) -> str:
    """计算一条记录的 hash（SHA256 十六进制）。

    Args:
        payload: 记录内容（可含 hash 字段，计算时会被排除）。
        previous_hash: 前一条记录的 hash；首条传 ``GENESIS_HASH``。
    """
    clean = {key: value for key, value in payload.items() if key not in _HASH_FIELDS}
    canonical = json.dumps(
        clean,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()


def verify_chain(records: Sequence[Mapping[str, Any]]) -> ChainVerification:
    """逐条校验 hash 链完整性；空链视为合法。

    Returns:
        ``ChainVerification``：合法时 ``valid=True``；断链时给出首个
        ``broken_index`` 与原因（前序 hash 不匹配 = 删除/换序；记录 hash
        不匹配 = 内容被篡改）。
    """
    expected_previous = GENESIS_HASH
    for index, record in enumerate(records):
        stored_previous = record.get("prev_record_hash")
        stored_hash = record.get("record_hash")
        if not isinstance(stored_previous, str) or not isinstance(stored_hash, str):
            return _broken(index, "记录缺少 prev_record_hash / record_hash")

        if stored_previous != expected_previous:
            return _broken(index, "前序 hash 不匹配（记录被删除或换序）")

        computed = compute_record_hash(record, stored_previous)
        if computed != stored_hash:
            return _broken(index, "记录 hash 不匹配（内容被篡改）")

        expected_previous = stored_hash

    return ChainVerification(valid=True, checked=len(records))


def _broken(index: int, reason: str) -> ChainVerification:
    """构造断链结果并写 CRITICAL 告警。"""
    logger.critical("hash chain broken | index={} reason={}", index, reason)
    return ChainVerification(
        valid=False,
        checked=index,
        broken_index=index,
        reason=reason,
    )
