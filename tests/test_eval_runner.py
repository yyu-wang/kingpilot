"""M2-T05：评测判定/统计逻辑单测（FakeProvider 零网络）+ 评测集完整性守卫。

守卫用例的「已注册工具集合」取自 build_default_registry() 的 spec 名——
不用 binding 常量再对一遍常量（那才是同义反复）：数据集期望值 ⊆ 注册表，
任何一边漂移都会红。
"""

from agent.tools import build_default_registry
from evals.dataset import QUESTIONS, EvalQuestion
from evals.runner import check_params, judge, run_evaluation
from infrastructure.exceptions import LLMUnavailableError
from models.llm import PlanResult, ToolCallDecision
from tests.fakes import FakeProvider

_SAL = "kingdee_query_sales_order"
_INV = "kingdee_query_inventory"


def _q(expected: str | None = _SAL, qid: str = "T-01") -> EvalQuestion:
    return EvalQuestion(
        question_id=qid, question="测试问题", expected_tool=expected, category="正向"
    )


def _plan(*tool_names: str) -> PlanResult:
    return PlanResult(
        tool_calls=[ToolCallDecision(tool_name=name, arguments={}) for name in tool_names]
    )


# ── judge 判定口径 ──


def test_judge_tool_match_passes() -> None:
    assert judge(_q(), _plan(_SAL)).passed is True


def test_judge_tool_mismatch_fails() -> None:
    assert judge(_q(), _plan(_INV)).passed is False


def test_judge_chat_empty_tool_calls_passes() -> None:
    """纯聊天题：LLM 不调工具 = 选对（Phase 1 自律性指标）。"""
    verdict = judge(_q(expected=None), _plan())
    assert verdict.passed is True
    assert verdict.actual_tool is None


def test_judge_chat_but_tool_called_fails() -> None:
    assert judge(_q(expected=None), _plan(_SAL)).passed is False


def test_judge_records_extra_calls() -> None:
    """只看第一个 tool_call 判定；多选记入 extra_calls 不扣分（M3 观察点）。"""
    verdict = judge(_q(), _plan(_SAL, _INV))
    assert verdict.passed is True
    assert verdict.extra_calls == 1


# ── check_params 参数校验 ──


def test_check_params_rejects_unknown_field() -> None:
    """schema extra=forbid：LLM 编造参数会被拒（参数通过率指标的样本）。"""
    assert check_params(build_default_registry(), _SAL, {"bogus": 1}) is False


def test_check_params_unknown_tool_returns_none() -> None:
    """幻觉工具名无 schema 可校验 → None（不适用）。"""
    assert check_params(build_default_registry(), "kingdee_query_bogus", {}) is None


# ── run_evaluation 集成（FakeProvider 剧本）──


async def test_run_evaluation_mixed_script() -> None:
    """对/错/ERROR 三题混合：错误不计分母，plan 收到全量工具声明。"""
    questions = [_q(qid="A"), _q(expected=_INV, qid="B"), _q(qid="C")]
    provider = FakeProvider(
        plan_queue=[
            _plan(_SAL),  # A 对
            _plan(_SAL),  # B 错：期望 INV，实际 SAL
            LLMUnavailableError("模拟超时"),  # C：ERROR 不计分母
        ]
    )

    report = await run_evaluation(provider, questions, build_default_registry())

    assert provider.plan_calls == 3
    assert report.error_count == 1
    assert report.scored_count == 2
    assert report.accuracy == 0.5  # A 对 / B 错，ERROR 不计分母
    assert report.verdicts[2].is_error is True
    assert report.verdicts[2].error_message == "模拟超时"


async def test_run_evaluation_params_valid_flag() -> None:
    """参数通过率：合法参数 True / 幻觉工具 None。"""
    provider = FakeProvider(
        plan_queue=[
            PlanResult(
                tool_calls=[ToolCallDecision(tool_name=_SAL, arguments={"bill_no": "SO001"})]
            ),
            _plan("kingdee_query_bogus"),
        ]
    )
    questions = [_q(qid="A"), _q(qid="B")]
    report = await run_evaluation(provider, questions, build_default_registry())

    assert report.verdicts[0].params_valid is True
    assert report.verdicts[1].params_valid is None


# ── _summarize 边界 ──


async def test_summarize_all_errors_zero_accuracy() -> None:
    """全 ERROR：scored 为空 → accuracy 取 0 防除零，不炸。"""
    provider = FakeProvider(plan_queue=[LLMUnavailableError("挂了")])
    report = await run_evaluation(provider, [_q()], build_default_registry())
    assert report.accuracy == 0.0
    assert report.scored_count == 0
    assert report.error_count == 1


async def test_summarize_per_tool_groups_chat_separately() -> None:
    """分工具统计按「期望工具」分组，纯聊天单列为 chat。"""
    provider = FakeProvider(plan_queue=[_plan(), _plan(_SAL)])
    questions = [_q(expected=None, qid="CHAT"), _q(qid="SAL")]
    report = await run_evaluation(provider, questions, build_default_registry())

    assert report.per_tool["chat"] == (1, 1)
    assert report.per_tool[_SAL] == (1, 1)


# ── 评测集完整性守卫 ──


def test_dataset_has_at_least_30_questions() -> None:
    assert len(QUESTIONS) >= 30  # 任务卡：≥30 条


def test_dataset_ids_unique() -> None:
    ids = [q.question_id for q in QUESTIONS]
    assert len(ids) == len(set(ids))


def test_dataset_expected_tools_all_registered() -> None:
    """期望值必须要么 None（纯聊天）要么是注册表里的真实工具名。"""
    registered = {spec.name for spec in build_default_registry().all_specs()}
    for q in QUESTIONS:
        assert q.expected_tool is None or q.expected_tool in registered, q.question_id


def test_dataset_covers_each_query_tool_and_chat() -> None:
    """查询工具各 ≥3 条正向覆盖，纯聊天 ≥3 条（写工具由专门场景测试覆盖）。"""
    counts: dict[str, int] = {}
    for q in QUESTIONS:
        key = q.expected_tool or "chat"
        counts[key] = counts.get(key, 0) + 1
    query_tools = {
        tool.name
        for tool in build_default_registry().all_tools()
        if tool.tool_type == "query" and not tool.is_local
    }
    for tool in query_tools:
        assert counts.get(tool, 0) >= 3, f"{tool} 覆盖不足"
    assert counts["chat"] >= 3
