"""M8-T03：发布门禁单测。

覆盖正/负向指标、绝对阈值、相对基线回退、缺失数据 fail-closed、
基线读写与门禁报告落盘。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from config.release_gate import GATE_RULES, GateRule
from governance.release_gate import (
    GateResult,
    MetricCheck,
    ReleaseGate,
    load_baseline,
    load_report_metrics,
    save_baseline,
    write_gate_report,
)
from infrastructure.exceptions import ReleaseGateError
from scripts.release_gate import main as gate_main


def _metrics(**overrides: float) -> dict[str, float]:
    base = {
        "tool_selection_accuracy": 1.0,
        "error_rate": 0.0,
        "avg_latency_ms": 800.0,
        "avg_cost_usd": 0.0004,
    }
    base.update(overrides)
    return base


_BASELINE = _metrics()


def _evaluate(
    metrics: dict[str, float], baseline: dict[str, float] | None = _BASELINE
) -> GateResult:
    return ReleaseGate().evaluate(metrics, baseline, dataset_version="v1")


def _check(result: GateResult, metric: str) -> MetricCheck:
    return next(check for check in result.checks if check.metric == metric)


# ── 正向指标（越高越好）──


def test_metrics_equal_baseline_pass() -> None:
    result = _evaluate(_metrics())
    assert result.passed is True
    assert result.dataset_version == "v1"
    assert all(check.passed for check in result.checks)


def test_accuracy_drop_beyond_tolerance_blocks() -> None:
    result = _evaluate(_metrics(tool_selection_accuracy=0.95))
    assert result.passed is False
    assert _check(result, "tool_selection_accuracy").passed is False
    assert "回退" in _check(result, "tool_selection_accuracy").reason


def test_accuracy_drop_within_tolerance_passes() -> None:
    result = _evaluate(_metrics(tool_selection_accuracy=0.99))
    assert result.passed is True


def test_accuracy_below_absolute_floor_blocks() -> None:
    low = _metrics(tool_selection_accuracy=0.85)
    result = _evaluate(low, baseline=low)
    assert result.passed is False
    assert "绝对下限" in _check(result, "tool_selection_accuracy").reason


# ── 负向指标（越低越好）──


def test_error_rate_ceiling_blocks() -> None:
    result = _evaluate(_metrics(error_rate=0.2))
    assert result.passed is False
    assert "绝对上限" in _check(result, "error_rate").reason


def test_error_rate_regression_blocks() -> None:
    result = _evaluate(_metrics(error_rate=0.05))
    assert result.passed is False
    assert "上升" in _check(result, "error_rate").reason


def test_latency_ceiling_blocks() -> None:
    result = _evaluate(_metrics(avg_latency_ms=6000.0))
    assert result.passed is False
    assert "绝对上限" in _check(result, "avg_latency_ms").reason


def test_latency_regression_blocks() -> None:
    result = _evaluate(_metrics(avg_latency_ms=1400.0))
    assert result.passed is False
    assert "上升" in _check(result, "avg_latency_ms").reason


def test_cost_ceiling_blocks() -> None:
    result = _evaluate(_metrics(avg_cost_usd=0.06))
    assert result.passed is False
    assert "绝对上限" in _check(result, "avg_cost_usd").reason


# ── 缺失数据 fail-closed ──


def test_missing_metric_blocks() -> None:
    metrics = _metrics()
    del metrics["avg_cost_usd"]
    result = _evaluate(metrics)
    assert result.passed is False
    assert "缺少该指标" in _check(result, "avg_cost_usd").reason


def test_missing_baseline_blocks() -> None:
    result = _evaluate(_metrics(), baseline=None)
    assert result.passed is False
    assert any("基线不存在" in reason for reason in result.blocked_reasons)


def test_baseline_missing_metric_blocks() -> None:
    baseline = _metrics()
    del baseline["error_rate"]
    result = _evaluate(_metrics(), baseline=baseline)
    assert result.passed is False
    assert "基线缺少该指标" in _check(result, "error_rate").reason


def test_extra_metrics_are_ignored() -> None:
    result = _evaluate(_metrics(guard_recall=0.5))
    assert result.passed is True


# ── 规则表校验 ──


def test_empty_rules_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        ReleaseGate(rules=[])


def test_gate_exposes_rules() -> None:
    assert ReleaseGate().rules == GATE_RULES


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric": "x", "direction": "min"},
        {"metric": "x", "direction": "max"},
        {"metric": "x", "direction": "min", "floor": 1.0, "ceiling": 2.0},
        {"metric": "x", "direction": "max", "ceiling": 1.0, "floor": 0.0},
        {"metric": "x", "direction": "min", "floor": 1.0, "max_regression": -0.1},
        {"metric": " ", "direction": "min", "floor": 1.0},
    ],
)
def test_invalid_rule_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GateRule(**kwargs)


# ── 基线读写与报告落盘 ──


def test_baseline_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"

    save_baseline(path, _metrics(), dataset_version="v1")

    assert load_baseline(path) == _metrics()
    assert not path.with_suffix(".json.tmp").exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["dataset_version"] == "v1"


def test_load_missing_baseline_returns_none(tmp_path: Path) -> None:
    assert load_baseline(tmp_path / "nope.json") is None


def test_bad_baseline_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="合法 JSON"):
        load_baseline(path)


def test_baseline_without_metrics_raises(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"other": 1}), encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="缺少 metrics"):
        load_baseline(path)


def test_non_object_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="根节点"):
        load_report_metrics(path)


def test_bool_metric_rejected(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"metrics": {"error_rate": True}}), encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="不是合法数值"):
        load_report_metrics(path)


def test_load_report_metrics(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"dataset_version": "v1", "metrics": _metrics()}), encoding="utf-8")

    metrics, version = load_report_metrics(path)

    assert version == "v1"
    assert metrics == _metrics()


def test_report_without_metrics_raises(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"dataset_version": "v1"}), encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="缺少 metrics"):
        load_report_metrics(path)


def test_report_with_non_numeric_metric_raises(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"metrics": {"error_rate": "high"}}), encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="不是合法数值"):
        load_report_metrics(path)


def test_write_gate_report(tmp_path: Path) -> None:
    result = _evaluate(_metrics(tool_selection_accuracy=0.5))
    path = tmp_path / "gate_report.json"

    write_gate_report(result, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["dataset_version"] == "v1"
    assert any(not check["passed"] for check in payload["checks"])


# ── CLI 退出码 ──


def _write_report(path: Path, metrics: dict[str, float]) -> Path:
    path.write_text(json.dumps({"dataset_version": "v1", "metrics": metrics}), encoding="utf-8")
    return path


def test_cli_update_baseline_then_pass(tmp_path: Path) -> None:
    report = _write_report(tmp_path / "report.json", _metrics())
    baseline = tmp_path / "baseline.json"
    output = tmp_path / "gate_report.json"

    assert (
        gate_main(["--report", str(report), "--baseline", str(baseline), "--update-baseline"]) == 0
    )
    assert baseline.exists()

    exit_code = gate_main(
        ["--report", str(report), "--baseline", str(baseline), "--output", str(output)]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True


def test_cli_regressed_report_is_blocked(tmp_path: Path) -> None:
    report = _write_report(tmp_path / "report.json", _metrics(tool_selection_accuracy=0.7))
    baseline = tmp_path / "baseline.json"
    save_baseline(baseline, _BASELINE, dataset_version="v1")
    output = tmp_path / "gate_report.json"

    exit_code = gate_main(
        ["--report", str(report), "--baseline", str(baseline), "--output", str(output)]
    )

    assert exit_code == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert any("tool_selection_accuracy" in check["metric"] for check in payload["checks"])


def test_cli_missing_report_returns_config_error(tmp_path: Path) -> None:
    assert gate_main(["--report", str(tmp_path / "nope.json")]) == 2
