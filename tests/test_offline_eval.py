"""M8-T01：离线评测框架单测。

覆盖版本化数据集加载校验、统一指标计算（准确率/延迟/成本）、
报告序列化，以及 M2 评测集迁移后的兼容性。
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from agent.llm import LLMProvider
from evals.dataset import DATASET_VERSION, QUESTIONS
from governance.evaluation.dataset import EvalQuestion, load_dataset
from governance.evaluation.offline_eval import (
    judge,
    report_to_dict,
    run_evaluation,
)
from infrastructure.exceptions import EvaluationError, LLMUnavailableError
from models.llm import LLMContext, PlanResult, ToolCallDecision, ToolSpec
from tests.fakes import FakeProvider

_SAL = "kingdee_query_sales_order"
_INV = "kingdee_query_inventory"


def _question(qid: str = "T-01", expected: str | None = _SAL) -> EvalQuestion:
    return EvalQuestion(
        question_id=qid,
        question="测试问题",
        expected_tool=expected,
        category="正向",
    )


def _plan(*tool_names: str) -> PlanResult:
    return PlanResult(
        tool_calls=[ToolCallDecision(tool_name=name, arguments={}) for name in tool_names]
    )


def _write_dataset(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _valid_payload() -> dict[str, Any]:
    return {
        "version": "v9",
        "cases": [
            {
                "question_id": "Q-01",
                "question": "查订单",
                "expected_tool": _SAL,
                "category": "正向",
            },
            {
                "question_id": "Q-02",
                "question": "你好",
                "expected_tool": None,
                "category": "纯聊天",
            },
        ],
    }


# ── 数据集加载与校验 ──


def test_load_valid_dataset(tmp_path: Path) -> None:
    dataset = load_dataset(_write_dataset(tmp_path, _valid_payload()), known_tools={_SAL})

    assert dataset.version == "v9"
    assert len(dataset.questions) == 2
    assert dataset.questions[0].expected_tool == _SAL
    assert dataset.questions[1].expected_tool is None


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="读取失败"):
        load_dataset(tmp_path / "nope.json")


def test_invalid_json_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(EvaluationError, match="合法 JSON"):
        load_dataset(path)


def test_missing_version_fails(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["version"] = ""
    with pytest.raises(EvaluationError, match="version"):
        load_dataset(_write_dataset(tmp_path, payload))


def test_empty_cases_fails(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["cases"] = []
    with pytest.raises(EvaluationError, match="cases"):
        load_dataset(_write_dataset(tmp_path, payload))


def test_duplicate_ids_fail(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["cases"][1]["question_id"] = payload["cases"][0]["question_id"]
    with pytest.raises(EvaluationError, match="ID 重复"):
        load_dataset(_write_dataset(tmp_path, payload))


def test_unknown_tool_fails(tmp_path: Path) -> None:
    with pytest.raises(EvaluationError, match="未注册"):
        load_dataset(_write_dataset(tmp_path, _valid_payload()), known_tools={"other"})


def test_non_string_expected_tool_fails(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["cases"][0]["expected_tool"] = 123
    with pytest.raises(EvaluationError, match="expected_tool"):
        load_dataset(_write_dataset(tmp_path, payload))


def test_root_must_be_object(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(EvaluationError, match="根节点"):
        load_dataset(path)


def test_case_must_be_object(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["cases"] = ["not-a-case"]
    with pytest.raises(EvaluationError, match="cases\\[0\\]"):
        load_dataset(_write_dataset(tmp_path, payload))


def test_empty_expected_tool_fails(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["cases"][0]["expected_tool"] = "  "
    with pytest.raises(EvaluationError, match="不能为空串"):
        load_dataset(_write_dataset(tmp_path, payload))


def test_missing_required_field_fails(tmp_path: Path) -> None:
    payload = _valid_payload()
    del payload["cases"][0]["category"]
    with pytest.raises(EvaluationError, match="category"):
        load_dataset(_write_dataset(tmp_path, payload))


def test_migrated_dataset_is_versioned_and_complete() -> None:
    assert DATASET_VERSION == "v1"
    assert len(QUESTIONS) == 43
    assert len({q.question_id for q in QUESTIONS}) == 43


# ── 指标计算 ──


class _CostedProvider(FakeProvider):
    """每次 plan 增加 0.001 成本，用于验证成本探针差值口径。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cost = 0.0

    async def plan(self, context: LLMContext, tools: Sequence[ToolSpec]) -> PlanResult:
        self.cost += 0.001
        return await super().plan(context, tools)


async def test_report_metrics_and_latency_cost() -> None:
    provider = _CostedProvider(plan_queue=[_plan(_SAL), _plan(_INV)])
    questions = [_question("A"), _question("B", expected=_INV)]

    report = await run_evaluation(
        provider,
        questions,
        dataset_version="v9",
        cost_probe=lambda: provider.cost,
    )

    assert report.dataset_version == "v9"
    assert report.accuracy == 1.0
    assert report.avg_cost_usd == pytest.approx(0.001)
    assert report.avg_latency_ms > 0.0
    assert report.metrics["tool_selection_accuracy"] == 1.0
    assert report.metrics["error_rate"] == 0.0
    assert report.metrics["avg_cost_usd"] == pytest.approx(0.001)


async def test_error_verdict_keeps_latency_and_cost() -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("超时")])

    report = await run_evaluation(provider, [_question()], cost_probe=lambda: 0.0)

    verdict = report.verdicts[0]
    assert verdict.is_error is True
    assert verdict.latency_ms >= 0.0
    assert verdict.cost_usd == 0.0
    assert report.metrics["error_rate"] == 1.0
    assert report.metrics["tool_selection_accuracy"] == 0.0


async def test_chat_plan_without_cost_probe() -> None:
    """纯聊天无 tool_calls：params 不适用；未注入成本探针时成本为 0。"""
    provider = FakeProvider(plan_queue=[_plan()])

    report = await run_evaluation(provider, [_question(expected=None)])

    verdict = report.verdicts[0]
    assert verdict.passed is True
    assert verdict.params_valid is None
    assert verdict.cost_usd == 0.0


async def test_empty_question_set_metrics_are_zero() -> None:
    report = await run_evaluation(FakeProvider(), [])

    assert report.metrics == {
        "tool_selection_accuracy": 0.0,
        "error_rate": 0.0,
        "avg_latency_ms": 0.0,
        "avg_cost_usd": 0.0,
    }


async def test_judge_keeps_legacy_semantics() -> None:
    assert judge(_question(), _plan(_SAL)).passed is True
    assert judge(_question(expected=None), _plan()).passed is True
    assert judge(_question(expected=None), _plan(_SAL)).passed is False
    verdict = judge(_question(), _plan(_SAL, _INV))
    assert verdict.passed is True
    assert verdict.extra_calls == 1


# ── 报告序列化 ──


async def test_report_to_dict_is_json_serializable() -> None:
    provider = FakeProvider(plan_queue=[_plan(_SAL)])
    report = await run_evaluation(
        provider, [_question()], dataset_version="v9", cost_probe=lambda: 0.0
    )

    payload = cast(dict[str, Any], report_to_dict(report, route_label="route=test"))

    encoded = json.dumps(payload, ensure_ascii=False)
    assert '"dataset_version": "v9"' in encoded
    assert payload["metrics"]["tool_selection_accuracy"] == 1.0
    assert payload["per_tool"][_SAL] == [1, 1]
    assert payload["questions"][0]["latency_ms"] >= 0.0
    assert payload["questions"][0]["cost_usd"] == 0.0
    assert payload["route"] == "route=test"


def test_llm_provider_protocol_still_satisfied() -> None:
    """框架以 LLMProvider 协议约束入参，FakeProvider 结构性满足。"""
    provider: LLMProvider = FakeProvider()
    assert provider is not None
