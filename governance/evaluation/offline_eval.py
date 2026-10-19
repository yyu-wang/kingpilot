"""离线评测框架（M8-T01）：批量执行 + 统一指标 + 报告。

在 M2-T05 判定口径上的扩展：
- 保留「首工具匹配 / 多选不扣分 / ERROR 不计分母」的原始口径
- 每题记录耗时与成本（成本探针可选，来自 M7 CostEngine）
- 报告统一暴露 metrics：选对率 / 错误率 / 平均延迟 / 平均成本

Why 把判定逻辑放 governance/：发布门禁（M8-T03）直接消费同一份
``EvaluationReport``，指标口径只在一处定义，避免脚本与门禁各算一套。
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from agent.llm import LLMProvider
from agent.tools import ToolRegistry, build_default_registry
from governance.evaluation.dataset import EvalQuestion
from infrastructure.exceptions import LLMError, ToolError
from models.llm import LLMContext, LLMMessage, PlanResult

_LATENCY_PRECISION = 3
_COST_PRECISION = 6


@dataclass(frozen=True)
class QuestionVerdict:
    """单题判定结果（ERROR 题只填 question/is_error/error_message）。"""

    question: EvalQuestion
    actual_tool: str | None = None
    passed: bool = False
    extra_calls: int = 0  # 多选记录在案，不扣分（M3 多步编排观察点）
    params_valid: bool | None = None  # None = 不适用（纯聊天或工具未注册）
    is_error: bool = False
    error_message: str = ""
    latency_ms: float = 0.0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class EvaluationReport:
    """汇总统计：选对率 = 选对 / (总数 - 错误数)。"""

    verdicts: tuple[QuestionVerdict, ...]
    accuracy: float
    scored_count: int
    error_count: int
    # 期望工具 → (选对数, 总题数)；纯聊天单列为 "chat"
    per_tool: dict[str, tuple[int, int]]
    dataset_version: str = ""

    @property
    def avg_latency_ms(self) -> float:
        """成功题的平均耗时（ERROR 题不参与，避免基础设施抖动污染）。"""
        values = [v.latency_ms for v in self.verdicts if not v.is_error]
        if not values:
            return 0.0
        return round(sum(values) / len(values), _LATENCY_PRECISION)

    @property
    def avg_cost_usd(self) -> float:
        """单题平均成本（含 ERROR 题的实际花费）。"""
        if not self.verdicts:
            return 0.0
        return round(
            sum(v.cost_usd for v in self.verdicts) / len(self.verdicts),
            _COST_PRECISION,
        )

    @property
    def metrics(self) -> dict[str, float]:
        """发布门禁统一消费的指标集合。"""
        total = len(self.verdicts)
        return {
            "tool_selection_accuracy": round(self.accuracy, _COST_PRECISION),
            "error_rate": round(self.error_count / total, _COST_PRECISION) if total else 0.0,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_cost_usd": self.avg_cost_usd,
        }


def judge(question: EvalQuestion, plan: PlanResult) -> QuestionVerdict:
    """判定单题：第一个 tool_call 匹配期望即选对（纯聊天 = 空 tool_calls）。"""
    actual = plan.tool_calls[0].tool_name if plan.tool_calls else None
    return QuestionVerdict(
        question=question,
        actual_tool=actual,
        passed=actual == question.expected_tool,
        extra_calls=max(len(plan.tool_calls) - 1, 0),
    )


def check_params(registry: ToolRegistry, tool_name: str, arguments: object) -> bool | None:
    """参数可校验性：实际工具的 schema 吃不吃得下 LLM 给的参数。

    Returns:
        True/False = 校验通过/失败；None = 工具名本身未注册（幻觉，无 schema 可校验）。
    """
    try:
        binding = registry.get(tool_name)
    except ToolError:
        return None
    try:
        binding.params_model.model_validate(arguments)
    except ValueError:
        return False
    return True


async def run_evaluation(
    provider: LLMProvider,
    questions: Sequence[EvalQuestion],
    registry: ToolRegistry | None = None,
    *,
    dataset_version: str = "",
    cost_probe: Callable[[], float] | None = None,
) -> EvaluationReport:
    """逐题跑 plan → 判定；LLM 异常记 ERROR 不计分母。

    Args:
        cost_probe: 可选成本探针（如 ``RoutingProvider.cost_engine.total_cost_usd``）；
            每题前后各读一次，差值记入该题成本。未提供则成本为 0。
    """
    reg = registry or build_default_registry()
    specs = reg.all_specs()
    verdicts: list[QuestionVerdict] = []
    for question in questions:
        started = time.perf_counter()
        cost_before = cost_probe() if cost_probe is not None else 0.0
        context = LLMContext(messages=[LLMMessage(role="user", content=question.question)])
        try:
            plan = await provider.plan(context, tools=specs)
        except LLMError as exc:
            # provider 层红线保证异常消息不含密钥；此处只留定位信息
            verdicts.append(
                QuestionVerdict(
                    question=question,
                    is_error=True,
                    error_message=str(exc),
                    latency_ms=_elapsed_ms(started),
                    cost_usd=_cost_delta(cost_probe, cost_before),
                )
            )
            continue
        verdict = judge(question, plan)
        params_valid: bool | None = None
        if plan.tool_calls:
            decision = plan.tool_calls[0]
            params_valid = check_params(reg, decision.tool_name, decision.arguments)
        verdicts.append(
            replace(
                verdict,
                params_valid=params_valid,
                latency_ms=_elapsed_ms(started),
                cost_usd=_cost_delta(cost_probe, cost_before),
            )
        )
    return _summarize(verdicts, dataset_version=dataset_version)


def report_to_dict(report: EvaluationReport, *, route_label: str = "") -> dict[str, object]:
    """报告 → JSON 可序列化字典（脚本落盘与门禁共用）。"""
    return {
        "dataset_version": report.dataset_version,
        "route": route_label,
        "metrics": report.metrics,
        "accuracy": report.accuracy,
        "scored_count": report.scored_count,
        "error_count": report.error_count,
        "per_tool": {tool: list(counts) for tool, counts in report.per_tool.items()},
        "questions": [
            {
                "id": v.question.question_id,
                "question": v.question.question,
                "expected": v.question.expected_tool,
                "actual": v.actual_tool,
                "passed": v.passed,
                "params_valid": v.params_valid,
                "is_error": v.is_error,
                "error": v.error_message,
                "latency_ms": v.latency_ms,
                "cost_usd": v.cost_usd,
            }
            for v in report.verdicts
        ],
    }


def _summarize(verdicts: list[QuestionVerdict], *, dataset_version: str) -> EvaluationReport:
    """选对率与分工具统计；scored 为空（全 ERROR）时 accuracy 取 0 防除零。"""
    scored = [v for v in verdicts if not v.is_error]
    accuracy = (sum(v.passed for v in scored) / len(scored)) if scored else 0.0
    per_tool: dict[str, tuple[int, int]] = {}
    for v in scored:
        key = v.question.expected_tool or "chat"
        passed, total = per_tool.get(key, (0, 0))
        per_tool[key] = (passed + (1 if v.passed else 0), total + 1)
    return EvaluationReport(
        verdicts=tuple(verdicts),
        accuracy=accuracy,
        scored_count=len(scored),
        error_count=len(verdicts) - len(scored),
        per_tool=per_tool,
        dataset_version=dataset_version,
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, _LATENCY_PRECISION)


def _cost_delta(cost_probe: Callable[[], float] | None, before: float) -> float:
    if cost_probe is None:
        return 0.0
    return round(cost_probe() - before, _COST_PRECISION)
