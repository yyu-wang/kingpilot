"""Tool 选对率评测的判定与统计（M2-T05）。

口径（设计 D1，用户已批）：
- **选对** = ``plan.tool_calls`` 第一个的 tool_name 与期望一致；纯聊天题 = 空 tool_calls
- 只看第一个 tool_call：31 题全部单工具可答（T04 决策 D1），第一个即主意图；
  多选记入明细（extra_calls）不扣分——多步编排是 M3 的观察点
- LLM 异常记 ERROR **不计分母**（基础设施抖动不污染模型能力指标），
  但报告必须显性展示，不让失败隐形
- 参数通过率是附加观察指标（不进验收线）：把第一个 tool_call 的参数喂给
  实际工具的 params_model 校验，暴露「工具选对但参数放不下」的缺口

Why 判定/统计与 CLI 分离：判定逻辑要过 mypy strict + 单测（FakeProvider 零
网络），脚本只留装配与渲染——与 scripts/ 薄入口惯例一致。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from agent.llm import LLMProvider
from agent.tools import ToolRegistry, build_default_registry
from evals.dataset import EvalQuestion
from infrastructure.exceptions import LLMError, ToolError
from models.llm import LLMContext, LLMMessage, PlanResult


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


@dataclass(frozen=True)
class EvaluationReport:
    """汇总统计：选对率 = 选对 / (总数 - 错误数)。"""

    verdicts: tuple[QuestionVerdict, ...]
    accuracy: float
    scored_count: int
    error_count: int
    # 期望工具 → (选对数, 总题数)；纯聊天单列为 "chat"
    per_tool: dict[str, tuple[int, int]]


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
) -> EvaluationReport:
    """逐题跑 plan → 判定；LLM 异常记 ERROR 不计分母。顺序执行（31 题无并发需求）。"""
    reg = registry or build_default_registry()
    specs = reg.all_specs()
    verdicts: list[QuestionVerdict] = []
    for question in questions:
        context = LLMContext(messages=[LLMMessage(role="user", content=question.question)])
        try:
            plan = await provider.plan(context, tools=specs)
        except LLMError as exc:
            # provider 层红线保证异常消息不含密钥；此处只留定位信息
            verdicts.append(
                QuestionVerdict(question=question, is_error=True, error_message=str(exc))
            )
            continue
        verdict = judge(question, plan)
        if plan.tool_calls:
            decision = plan.tool_calls[0]
            verdict = QuestionVerdict(
                question=question,
                actual_tool=verdict.actual_tool,
                passed=verdict.passed,
                extra_calls=verdict.extra_calls,
                params_valid=check_params(reg, decision.tool_name, decision.arguments),
            )
        verdicts.append(verdict)
    return _summarize(verdicts)


def _summarize(verdicts: list[QuestionVerdict]) -> EvaluationReport:
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
    )
