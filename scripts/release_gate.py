"""发布门禁 CLI（M8-T03）。

用法：
    # 首次建立基线（显式操作）
    uv run python scripts/release_gate.py --report evals/reports/latest.json --update-baseline

    # 发布前门禁
    make release-gate REPORT=evals/reports/latest.json

退出码：0 放行 / 1 阻断 / 2 配置或文件错误。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from governance.release_gate import (
    GateResult,
    ReleaseGate,
    load_baseline,
    load_report_metrics,
    save_baseline,
    write_gate_report,
)
from infrastructure.exceptions import ReleaseGateError

_DEFAULT_BASELINE = Path("evals/baselines/latest.json")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KingPilot 发布门禁")
    parser.add_argument("--report", required=True, help="M8-T01 评测报告 JSON 路径")
    parser.add_argument(
        "--baseline",
        default=str(_DEFAULT_BASELINE),
        help=f"基线路径（默认 {_DEFAULT_BASELINE}）",
    )
    parser.add_argument("--output", default="", help="门禁报告输出路径（默认报告同目录）")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="用当前报告覆盖基线（显式操作，不参与门禁判定）",
    )
    return parser.parse_args(argv)


def _render(result: GateResult) -> None:
    print("=" * 72)
    print(f"发布门禁：{'PASSED' if result.passed else 'BLOCKED'} | 数据集 {result.dataset_version}")
    print("=" * 72)
    for check in result.checks:
        value = "-" if check.value is None else f"{check.value:.6f}"
        baseline = "-" if check.baseline is None else f"{check.baseline:.6f}"
        mark = "PASS" if check.passed else "FAIL"
        print(
            f"[{mark}] {check.metric:<28} value={value:<12} baseline={baseline:<12} {check.reason}"
        )
    for reason in result.blocked_reasons:
        print(f"阻断原因: {reason}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report_path = Path(args.report)
    baseline_path = Path(args.baseline)
    try:
        metrics, dataset_version = load_report_metrics(report_path)
        if args.update_baseline:
            save_baseline(baseline_path, metrics, dataset_version=dataset_version)
            print(f"基线已更新：{baseline_path}")
            return 0

        baseline = load_baseline(baseline_path)
        result = ReleaseGate().evaluate(metrics, baseline, dataset_version=dataset_version)
        output = Path(args.output) if args.output else report_path.parent / "gate_report.json"
        write_gate_report(result, output)
        _render(result)
        print(f"门禁报告已写入 {output}")
        return 0 if result.passed else 1
    except ReleaseGateError as exc:
        print(f"门禁配置错误：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
