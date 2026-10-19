"""M8-T05：跨模块不变量守卫。

这些断言不重复业务单测，只守住模块之间的接口契约：
- 评测指标 ↔ 门禁规则
- Prompt 配置 ↔ 版本文件
- 评测集 ↔ 工具注册表
- 在线抽样默认关闭
- 门禁规则合法且唯一
- 灰度选择跨实例确定性
"""

import json
from pathlib import Path

from agent.tools import build_default_registry
from config.release_gate import GATE_RULES
from config.settings import Settings
from evals.dataset import DATASET_VERSION, QUESTIONS
from governance.evaluation.offline_eval import EvaluationReport
from governance.prompt_registry import PromptRegistry

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_gate_metrics_are_produced_by_eval_report() -> None:
    """门禁消费的每个指标必须由评测报告产出（防两边字段漂移）。"""
    report = EvaluationReport(
        verdicts=(),
        accuracy=0.0,
        scored_count=0,
        error_count=0,
        per_tool={},
    )
    assert {rule.metric for rule in GATE_RULES} <= set(report.metrics)


def test_prompt_registry_covers_all_prompt_files() -> None:
    """三条 Prompt 都必须在 rollout.json 中登记且 active 版本文件存在。"""
    registry = PromptRegistry(_PROJECT_ROOT / "prompts")
    for prompt_id in ("reflect", "generate", "summarize"):
        assert registry.active_version(prompt_id)
        assert registry.list_versions(prompt_id)


def test_eval_dataset_tools_are_all_registered() -> None:
    """评测集期望工具必须全部存在于当前注册表。"""
    assert DATASET_VERSION == "v1"
    known = {spec.name for spec in build_default_registry().all_specs()}
    missing = {
        question.expected_tool
        for question in QUESTIONS
        if question.expected_tool is not None and question.expected_tool not in known
    }
    assert missing == set()


def test_online_sampling_is_off_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.online_sample_rate == 0.0
    assert settings.online_sample_max_per_day > 0


def test_gate_rules_are_valid_and_unique() -> None:
    metrics = [rule.metric for rule in GATE_RULES]
    assert len(metrics) == len(set(metrics))
    for rule in GATE_RULES:
        assert rule.direction in {"min", "max"}
        assert rule.max_regression >= 0.0
        if rule.direction == "min":
            assert rule.floor is not None and rule.ceiling is None
        else:
            assert rule.ceiling is not None and rule.floor is None


def test_prompt_rollout_is_deterministic_across_instances(tmp_path: Path) -> None:
    root = tmp_path / "prompts"
    (root / "reflect").mkdir(parents=True)
    (root / "reflect" / "v1.txt").write_text("V1", encoding="utf-8")
    (root / "reflect" / "v2.txt").write_text("V2", encoding="utf-8")
    (root / "rollout.json").write_text(
        json.dumps({"reflect": {"active": "v1", "rollout": {"v2": 0.5}}}),
        encoding="utf-8",
    )

    first = PromptRegistry(root)
    second = PromptRegistry(root)
    for index in range(50):
        key = f"session-{index}"
        assert first.get("reflect", rollout_key=key).version == (
            second.get("reflect", rollout_key=key).version
        )
