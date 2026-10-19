"""发布门禁（M8-T03）：读取评测报告 → 对比基线 → 阻断/放行。

输入是 M8-T01 的 ``report.metrics``，输出是可归档的门禁报告：
- 逐指标记录 value / baseline / 是否通过 / 原因
- 缺指标、缺基线、坏文件一律 fail-closed
- 基线只能通过显式 ``--update-baseline`` 更新，避免门禁自我放水
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.release_gate import GATE_RULES, GateRule
from infrastructure.exceptions import ReleaseGateError

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MetricCheck:
    """单个指标的判定结果。"""

    metric: str
    value: float | None
    baseline: float | None
    passed: bool
    reason: str


@dataclass(frozen=True)
class GateResult:
    """一次门禁判定的完整结果。"""

    passed: bool
    checks: tuple[MetricCheck, ...]
    blocked_reasons: tuple[str, ...]
    dataset_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化字典（归档用）。"""
        return {
            "schema_version": _SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "passed": self.passed,
            "dataset_version": self.dataset_version,
            "blocked_reasons": list(self.blocked_reasons),
            "checks": [asdict(check) for check in self.checks],
        }


class ReleaseGate:
    """规则表驱动的发布门禁。"""

    def __init__(self, rules: Sequence[GateRule] = GATE_RULES) -> None:
        if not rules:
            raise ValueError("门禁规则不能为空（fail-closed）")
        self._rules = tuple(rules)

    @property
    def rules(self) -> tuple[GateRule, ...]:
        return self._rules

    def evaluate(
        self,
        metrics: Mapping[str, float],
        baseline: Mapping[str, float] | None,
        *,
        dataset_version: str = "",
    ) -> GateResult:
        """逐指标判定；缺失与回退都进 blocked_reasons。"""
        blocked: list[str] = []
        if baseline is None:
            blocked.append("基线不存在：请先用 --update-baseline 建立基线")

        checks: list[MetricCheck] = []
        for rule in self._rules:
            value = _as_float(metrics.get(rule.metric))
            if value is None:
                checks.append(MetricCheck(rule.metric, None, None, False, "报告缺少该指标"))
                continue

            base: float | None = None
            if baseline is not None:
                base = _as_float(baseline.get(rule.metric))
                if base is None:
                    checks.append(MetricCheck(rule.metric, value, None, False, "基线缺少该指标"))
                    continue

            reasons = _rule_violations(rule, value, base)
            checks.append(
                MetricCheck(
                    metric=rule.metric,
                    value=value,
                    baseline=base,
                    passed=not reasons,
                    reason="；".join(reasons) if reasons else "通过",
                )
            )

        passed = not blocked and all(check.passed for check in checks)
        return GateResult(
            passed=passed,
            checks=tuple(checks),
            blocked_reasons=tuple(blocked),
            dataset_version=dataset_version,
        )


def load_report_metrics(path: Path) -> tuple[dict[str, float], str]:
    """读取 M8-T01 报告，返回 (metrics, dataset_version)。"""
    payload = _read_json(path, what="评测报告")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ReleaseGateError(f"评测报告缺少 metrics: {path}")
    parsed = _parse_metrics(metrics, source="评测报告")
    version = str(payload.get("dataset_version") or "")
    return parsed, version


def load_baseline(path: Path) -> dict[str, float] | None:
    """读取基线；文件不存在返回 None（由门禁判 fail-closed）。"""
    if not path.exists():
        return None
    payload = _read_json(path, what="门禁基线")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ReleaseGateError(f"门禁基线缺少 metrics: {path}")
    return _parse_metrics(metrics, source="门禁基线")


def save_baseline(path: Path, metrics: Mapping[str, float], *, dataset_version: str = "") -> Path:
    """显式写入新基线（原子写，避免半截文件）。"""
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "updated_at": datetime.now(UTC).isoformat(),
        "metrics": _parse_metrics(metrics, source="新基线"),
    }
    _atomic_write_json(path, payload)
    return path


def write_gate_report(result: GateResult, path: Path) -> Path:
    """门禁报告落盘（原子写）。"""
    _atomic_write_json(path, result.to_dict())
    return path


def _rule_violations(rule: GateRule, value: float, baseline: float | None) -> list[str]:
    """按方向计算绝对阈值与相对基线退化。"""
    reasons: list[str] = []
    if rule.direction == "min":
        if rule.floor is not None and value < rule.floor:
            reasons.append(f"低于绝对下限 {rule.floor}")
        if baseline is not None and value < baseline - rule.max_regression:
            reasons.append(f"较基线回退超过 {rule.max_regression}")
    else:
        if rule.ceiling is not None and value > rule.ceiling:
            reasons.append(f"高于绝对上限 {rule.ceiling}")
        if baseline is not None and value > baseline + rule.max_regression:
            reasons.append(f"较基线上升超过 {rule.max_regression}")
    return reasons


def _parse_metrics(raw: Mapping[str, Any], *, source: str) -> dict[str, float]:
    """指标必须是有限数值；bool/字符串/None 一律拒绝。"""
    parsed: dict[str, float] = {}
    for key, value in raw.items():
        number = _as_float(value)
        if number is None:
            raise ReleaseGateError(f"{source} 指标 {key} 不是合法数值: {value!r}")
        parsed[str(key)] = number
    return parsed


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _read_json(path: Path, *, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseGateError(f"{what}读取失败: {path}") from exc
    except ValueError as exc:
        raise ReleaseGateError(f"{what}不是合法 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReleaseGateError(f"{what}根节点必须是对象: {path}")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(path)
