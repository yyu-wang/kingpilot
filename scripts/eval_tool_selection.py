"""Tool 选对率评测脚本（M8-T01：统一评测框架的薄入口）。

只评 LLM 选工具这一步（provider.plan），不执行工具、不连金蝶沙箱；
判定口径与指标计算见 ``governance/evaluation/offline_eval.py``。
验收线选对率 > 90%（Step 2 验收标准）。

用法：
    make eval                        # 用 .env 当前路由（报告头会显式打印模型路由）
    make eval ROUTE=chat_default     # 指定路由覆盖 .env（评测务必用 DeepSeek——
                                     # 本地小模型 function calling 波动会污染指标）
    uv run python scripts/eval_tool_selection.py --output report.json

密钥红线：API key 只从 .env 读，永不作为命令行参数（会进 shell history）。
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

from agent.providers import get_provider
from agent.tools import build_default_registry
from config.settings import get_settings
from evals.dataset import DATASET_VERSION, QUESTIONS
from governance.cost_engine import RoutingProvider
from governance.evaluation.offline_eval import (
    EvaluationReport,
    QuestionVerdict,
    report_to_dict,
    run_evaluation,
)

# Step 2 验收标准：选对率 > 90%；未达标脚本以非零码退出（未来 CI 门禁直接可用）
_ACCURACY_THRESHOLD = 0.9


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tool 选对率评测（M2-T05）")
    parser.add_argument(
        "--route", default="", help="覆盖 .env 的 LLM 路由逻辑名（如 chat_default）"
    )
    parser.add_argument("--output", default="", help="可选：报告 JSON 落盘路径")
    return parser.parse_args()


def _print_verdict(v: QuestionVerdict) -> None:
    """单题明细行：期望/实际/多选/参数校验。"""
    expected = v.question.expected_tool or "（不调工具）"
    if v.is_error:
        mark, actual = "ERROR", v.error_message[:40]
    else:
        mark = "PASS" if v.passed else "FAIL"
        actual = v.actual_tool or "（未调工具）"
    extra = f" extra={v.extra_calls}" if v.extra_calls else ""
    params = (
        f" params={'ok' if v.params_valid else 'rejected'}" if v.params_valid is not None else ""
    )
    print(f"{v.question.question_id:<8} [{mark:^5}] {v.question.question}")
    print(f"{'':<8} 期望={expected:<28} 实际={actual}{extra}{params}")


def _render(report: EvaluationReport, route_label: str) -> None:
    """控制台明细 + 汇总（纯 print 对齐，不引渲染依赖）。"""
    print("=" * 72)
    print(
        f"KingPilot Tool 选对率评测 | {route_label} | "
        f"数据集 {DATASET_VERSION} | 问题数 {len(QUESTIONS)}"
    )
    print("=" * 72)
    for v in report.verdicts:
        _print_verdict(v)
    print("-" * 72)
    for tool, (passed, total) in sorted(report.per_tool.items()):
        print(f"  {tool:<30} {passed}/{total}")
    print("-" * 72)
    print(
        f"总计: {len(QUESTIONS)} 题 | 计分 {report.scored_count} | 错误 {report.error_count}（不计分母）"
    )
    print(f"选对率: {report.accuracy:.1%}  (阈值 {_ACCURACY_THRESHOLD:.0%})")
    print(f"平均延迟: {report.avg_latency_ms:.1f} ms | 平均成本: ${report.avg_cost_usd:.6f}")


def _dump_json(report: EvaluationReport, output: str, route_label: str) -> None:
    """报告落盘（--output 指定路径时），明细与指标全量可回放。"""
    payload = report_to_dict(report, route_label=route_label)
    Path(output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {output}")


async def main() -> int:
    args = _parse_args()
    settings = get_settings()
    if args.route:
        settings = settings.model_copy(update={"llm_route": args.route})
    provider = get_provider(settings)
    # 自动路由（RoutingProvider）时把成本账本作为探针，报告可给出单题成本
    cost_probe: Callable[[], float] | None = None
    if isinstance(provider, RoutingProvider):
        cost_probe = lambda: provider.cost_engine.total_cost_usd  # noqa: E731
    try:
        report = await run_evaluation(
            provider,
            QUESTIONS,
            build_default_registry(),
            dataset_version=DATASET_VERSION,
            cost_probe=cost_probe,
        )
    finally:
        await provider.aclose()

    route_label = (
        f"route={settings.llm_route or 'auto'} tier={settings.llm_tier} "
        f"temp={settings.llm_temperature}"
    )
    _render(report, route_label)
    if args.output:
        _dump_json(report, args.output, route_label)
    return 0 if report.accuracy > _ACCURACY_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
