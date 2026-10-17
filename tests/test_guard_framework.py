"""M4-T01：Guard 框架单测。

覆盖验收点：五个假 Guard 的顺序执行 / 短路 / 异常兜底（fail-closed），
外加空管道拒绝、拦截原因必填、结果可 JSON 序列化等边界。
"""

import json

import pytest

from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline, GuardResult


def _context() -> GuardContext:
    return GuardContext(
        tool_name="kingdee_query_sales_order",
        arguments={"bill_no": "SO000008"},
        user_context=UserContext(user_id="u001", role="viewer"),
        trace_id="trace-1",
    )


class FakeGuard:
    """可控假 Guard：共享 journal 记录调用顺序，支持异常与非法返回值。"""

    def __init__(
        self,
        name: str,
        journal: list[str],
        outcome: GuardResult | Exception | None = None,
        *,
        invalid: bool = False,
    ) -> None:
        self.name = name
        self._journal = journal
        self._outcome = outcome if outcome is not None else GuardResult.allow()
        self._invalid = invalid

    async def check(self, context: GuardContext) -> GuardResult:
        self._journal.append(self.name)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if self._invalid:
            return None  # type: ignore[return-value]
        return self._outcome


# ── 顺序与短路 ──


async def test_pipeline_runs_all_guards_in_order() -> None:
    """五个 Guard 全通过：按注册序执行，逐条留痕。"""
    journal: list[str] = []
    guards = [FakeGuard(f"g{index}", journal) for index in range(1, 6)]
    pipeline = GuardPipeline(guards)

    result = await pipeline.check(_context())

    assert journal == ["g1", "g2", "g3", "g4", "g5"]
    assert result.blocked is False
    assert result.reason == ""
    assert result.blocked_by == ""
    assert [record.guard_name for record in result.records] == journal
    assert all(record.passed for record in result.records)
    assert pipeline.guard_names == ("g1", "g2", "g3", "g4", "g5")


async def test_pipeline_short_circuits_after_block() -> None:
    """第 2 个 Guard 拦截：后续 Guard 不再执行，记录停在拦截点。"""
    journal: list[str] = []
    block = GuardResult.block("字段越权", detail={"field": "FSecret"})
    guards = [
        FakeGuard("g1", journal),
        FakeGuard("g2", journal, block),
        FakeGuard("g3", journal),
        FakeGuard("g4", journal),
        FakeGuard("g5", journal),
    ]

    result = await GuardPipeline(guards).check(_context())

    assert journal == ["g1", "g2"]
    assert result.blocked is True
    assert result.blocked_by == "g2"
    assert result.reason == "字段越权"
    assert len(result.records) == 2
    assert result.records[1].passed is False
    assert result.records[1].detail == {"field": "FSecret"}


async def test_first_guard_block_stops_immediately() -> None:
    """首卡拦截边界：只产生 1 条记录，后四个不执行。"""
    journal: list[str] = []
    guards = [
        FakeGuard("g1", journal, GuardResult.block("未注册工具")),
        *(FakeGuard(f"g{index}", journal) for index in range(2, 6)),
    ]

    result = await GuardPipeline(guards).check(_context())

    assert journal == ["g1"]
    assert result.blocked is True
    assert result.blocked_by == "g1"
    assert len(result.records) == 1


# ── 异常兜底（fail-closed）──


async def test_guard_exception_is_converted_to_block() -> None:
    """Guard 抛异常 → 按拦截处理，后续 Guard 不执行，程序不中断。"""
    journal: list[str] = []
    guards = [
        FakeGuard("g1", journal),
        FakeGuard("g2", journal, RuntimeError("规则表缺失")),
        FakeGuard("g3", journal),
    ]

    result = await GuardPipeline(guards).check(_context())

    assert journal == ["g1", "g2"]
    assert result.blocked is True
    assert result.blocked_by == "g2"
    assert "RuntimeError" in result.reason
    assert "规则表缺失" in result.reason
    assert result.records[1].detail == {"exception": "RuntimeError"}


async def test_invalid_guard_return_is_blocked() -> None:
    """实现方违约返回非 GuardResult：无法判定 = 拒绝。"""
    journal: list[str] = []

    result = await GuardPipeline([FakeGuard("bad", journal, invalid=True)]).check(_context())

    assert result.blocked is True
    assert result.blocked_by == "bad"
    assert "非法结果" in result.reason
    assert result.records[0].detail == {"returned_type": "NoneType"}


async def test_pipeline_is_reusable_and_stateless() -> None:
    """同一管道可重复调用，结论一致（Guard 无状态）。"""
    journal: list[str] = []
    pipeline = GuardPipeline([FakeGuard("g1", journal)])

    first = await pipeline.check(_context())
    second = await pipeline.check(_context())

    assert journal == ["g1", "g1"]
    assert first.records == second.records


# ── 装配与结果模型边界 ──


def test_empty_pipeline_rejected() -> None:
    """无 Guard = 无校验：装配期直接拒绝。"""
    with pytest.raises(ValueError, match="不能为空"):
        GuardPipeline([])


@pytest.mark.parametrize("reason", ["", "   "])
def test_block_requires_reason(reason: str) -> None:
    """拦截原因不得为空（静默拒绝不可排查）。"""
    with pytest.raises(ValueError, match="必须携带原因"):
        GuardResult.block(reason)


def test_allow_and_block_carry_detail() -> None:
    allowed = GuardResult.allow(detail={"checked": 3})
    assert allowed.passed is True
    assert allowed.reason == ""
    assert allowed.detail == {"checked": 3}

    blocked = GuardResult.block("缺参", detail={"missing": ["bill_no"]})
    assert blocked.passed is False
    assert blocked.reason == "缺参"
    assert blocked.detail == {"missing": ["bill_no"]}


async def test_to_records_is_json_serializable() -> None:
    """管道记录可直接进 trace / 审计（无自定义对象泄漏）。"""
    journal: list[str] = []
    result = await GuardPipeline(
        [FakeGuard("g1", journal, GuardResult.allow(detail={"checked": 1}))]
    ).check(_context())

    records = result.to_records()

    assert json.loads(json.dumps(records)) == [
        {"guard": "g1", "passed": True, "reason": "", "detail": {"checked": 1}}
    ]
