"""在线流量抽样（M8-T02）：按比例脱敏落盘，可回流离线评测集。

采样策略：
- ``sample_rate == 0``：完全关闭（连异常也不采，避免开发环境噪音）
- ``sample_rate > 0``：普通请求按比例采样；异常请求（阻断 / 降级 /
  工具执行错误）100% 采样，保证坏样本不丢
- 每日上限：超过 ``max_per_day`` 后停止写入（防磁盘失控）

脱敏红线：样本落盘前递归掩码所有字符串字段，并用 DLP 再扫一遍；
命中则拒写（fail-closed），绝不让原始 PII 进入样本目录。
"""

import json
import random
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from models.agent import AgentResponse, AgentTrace
from safety.datasecurity import redact_text
from safety.dlp import DlpScanner

_SCHEMA_VERSION = 1


class OnlineSampler:
    """JSONL 在线样本采样器（一天一个文件）。"""

    def __init__(
        self,
        root: Path,
        *,
        sample_rate: float,
        max_per_day: int = 500,
        clock: Callable[[], datetime] | None = None,
        rng: Callable[[], float] | None = None,
        scanner: DlpScanner | None = None,
    ) -> None:
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("采样率必须在 [0, 1] 区间")
        if max_per_day < 1:
            raise ValueError("每日样本上限必须大于 0")
        self._root = root
        self._sample_rate = sample_rate
        self._max_per_day = max_per_day
        self._clock = clock or (lambda: datetime.now(UTC))
        self._rng = rng or random.random
        self._scanner = scanner or DlpScanner()
        self._lock = threading.Lock()
        self._day = ""
        self._count = 0

    @property
    def enabled(self) -> bool:
        """采样率为 0 时整体关闭。"""
        return self._sample_rate > 0.0

    def maybe_sample(self, response: AgentResponse, *, session_id: str = "") -> Path | None:
        """按策略决定是否落盘；返回写入路径，未采样/失败返回 None。

        写入失败只告警，不影响主对话（采样是旁路能力）。
        """
        if not self.enabled or response.trace is None:
            return None
        trace = response.trace
        forced = _is_abnormal(response, trace)
        if not forced and self._rng() >= self._sample_rate:
            return None

        payload = self._build_payload(response, session_id=session_id, forced=forced)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if self._scanner.scan(line).blocked:
            logger.warning("在线样本命中 DLP，拒绝落盘 | trace_id={}", response.trace_id)
            return None

        try:
            return self._append(line)
        except OSError as exc:
            logger.warning("在线样本写入失败（不影响对话）: {}", exc)
            return None

    def to_eval_case_candidates(self, path: Path) -> list[dict[str, Any]]:
        """读取样本文件，产出可导入离线评测集的草稿（期望工具待人工标注）。"""
        if not path.exists():
            return []
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            sample = _parse_sample(line)
            if sample is None:
                continue
            trace = sample.get("trace")
            if not isinstance(trace, dict):
                continue
            question = str(trace.get("user_input") or "").strip()
            if not question or question in seen:
                continue
            seen.add(question)
            candidates.append(
                {
                    "question_id": f"ONLINE-{len(candidates) + 1:04d}",
                    "question": question,
                    "expected_tool": None,
                    "category": "在线样本（待标注）",
                }
            )
        return candidates

    def _build_payload(
        self, response: AgentResponse, *, session_id: str, forced: bool
    ) -> dict[str, Any]:
        """构造样本：先整体脱敏，再序列化。"""
        trace = response.trace
        assert trace is not None  # 调用方已判空
        raw = {
            "schema_version": _SCHEMA_VERSION,
            "sampled_at": self._clock().isoformat(),
            "forced": forced,
            "session_id": session_id,
            "content": response.content,
            "blocked": response.blocked,
            "cancelled": response.cancelled,
            "hallucination_warning": response.hallucination_warning,
            "trace": trace.model_dump(mode="json"),
        }
        return cast(dict[str, Any], _redact_tree(raw))

    def _append(self, line: str) -> Path | None:
        """按天写入 JSONL；同一进程内加锁并计数（超限返回 None）。"""
        day = self._clock().date().isoformat()
        with self._lock:
            if day != self._day:
                self._day = day
                self._count = 0
            if self._count >= self._max_per_day:
                logger.warning("在线样本已达每日上限 {}，停止采样", self._max_per_day)
                return None
            self._root.mkdir(parents=True, exist_ok=True)
            path = self._root / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._count += 1
            return path


def _is_abnormal(response: AgentResponse, trace: AgentTrace) -> bool:
    """异常样本判定：阻断 / 降级 / 任一工具执行失败。"""
    if response.blocked or trace.used_fallback:
        return True
    return any(execution.error is not None for execution in trace.tool_executions)


def _redact_tree(value: Any) -> Any:
    """递归掩码字符串（dict/list 均处理），结构化数字保持原样。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    return value


def _parse_sample(line: str) -> dict[str, Any] | None:
    """解析单行样本；坏行跳过并告警（单条坏数据不影响整批）。"""
    if not line.strip():
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        logger.warning("在线样本坏行，跳过")
        return None
    return payload if isinstance(payload, dict) else None


def iter_samples(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读取样本（坏行跳过），供后续治理脚本复用。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        sample = _parse_sample(line)
        if sample is not None:
            yield sample
