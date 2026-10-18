"""M5-T02：Saga 编排器单测。

覆盖成功路径、失败补偿（倒序）、补偿失败、无映射/不可逆人工介入、
显式补偿覆盖、状态迁移历史与幂等重跑。
"""

import json
from collections.abc import Mapping
from typing import Any

import pytest

from kingdee.idempotency import IdempotencyService
from kingdee.saga import SagaOrchestrator, SagaResult, SagaStep, SagaStepResult
from models.base import SagaStatus


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeExecutor:
    """记录调用顺序；可指定正向/补偿工具失败。"""

    def __init__(
        self,
        fail_forward: set[str] | None = None,
        fail_compensation: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail_forward = fail_forward or set()
        self._fail_compensation = fail_compensation or set()

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(arguments)
        self.calls.append((tool_name, payload))
        if tool_name in self._fail_forward or tool_name in self._fail_compensation:
            raise RuntimeError(f"{tool_name} failed")
        return {"tool": tool_name, "arguments": payload}


_MAP: dict[tuple[str, str], str] = {
    ("create", "save"): "comp_create",
    ("submit", "submit"): "comp_submit",
    ("audit", "audit"): "comp_audit",
}


def _steps() -> list[SagaStep]:
    return [
        SagaStep("s1", "create", {"bill": "SO1"}, action="save"),
        SagaStep("s2", "submit", {"bill": "SO1"}, action="submit"),
        SagaStep("s3", "audit", {"bill": "SO1"}, action="audit"),
    ]


def _orchestrator(
    *,
    compensate_map: dict[tuple[str, str], str] | None = None,
    no_auto_compensate: frozenset[str] | None = None,
    idempotency: IdempotencyService | None = None,
) -> SagaOrchestrator:
    return SagaOrchestrator(
        idempotency=idempotency or IdempotencyService(ttl_seconds=3600, clock=FakeClock()),
        compensate_map=_MAP if compensate_map is None else compensate_map,
        no_auto_compensate=no_auto_compensate,
    )


# ── 成功路径 ──


async def test_all_steps_succeed() -> None:
    executor = FakeExecutor()

    result = await _orchestrator().execute(_steps(), executor, saga_id="saga-1", trace_id="trace-1")

    assert result.status == SagaStatus.SUCCESS
    assert [item.status for item in result.step_results] == [
        SagaStatus.SUCCESS,
        SagaStatus.SUCCESS,
        SagaStatus.SUCCESS,
    ]
    assert [name for name, _ in executor.calls] == ["create", "submit", "audit"]
    assert result.status_history == (
        SagaStatus.PENDING,
        SagaStatus.RUNNING,
        SagaStatus.SUCCESS,
    )


# ── 失败补偿 ──


async def test_second_step_failure_compensates_first() -> None:
    executor = FakeExecutor(fail_forward={"submit"})

    result = await _orchestrator().execute(_steps(), executor, saga_id="saga-2")

    assert result.status == SagaStatus.ROLLED_BACK
    assert result.step_results[1].status == SagaStatus.FAILED
    assert [item.tool_name for item in result.compensation_results] == ["comp_create"]
    assert result.compensation_results[0].status == SagaStatus.COMPENSATED
    assert [name for name, _ in executor.calls] == [
        "create",
        "submit",
        "comp_create",
    ]
    assert result.status_history == (
        SagaStatus.PENDING,
        SagaStatus.RUNNING,
        SagaStatus.FAILED,
        SagaStatus.COMPENSATING,
        SagaStatus.ROLLED_BACK,
    )


async def test_compensation_runs_in_reverse_order() -> None:
    executor = FakeExecutor(fail_forward={"audit"})

    result = await _orchestrator().execute(_steps(), executor, saga_id="saga-3")

    assert result.status == SagaStatus.ROLLED_BACK
    assert [item.tool_name for item in result.compensation_results] == [
        "comp_submit",
        "comp_create",
    ]
    assert [name for name, _ in executor.calls] == [
        "create",
        "submit",
        "audit",
        "comp_submit",
        "comp_create",
    ]


async def test_first_step_failure_has_nothing_to_compensate() -> None:
    executor = FakeExecutor(fail_forward={"create"})

    result = await _orchestrator().execute(_steps(), executor, saga_id="saga-4")

    assert result.status == SagaStatus.FAILED
    assert result.compensation_results == ()
    assert [name for name, _ in executor.calls] == ["create"]
    assert result.status_history == (
        SagaStatus.PENDING,
        SagaStatus.RUNNING,
        SagaStatus.FAILED,
    )


async def test_compensation_failure_marks_transaction_failed() -> None:
    executor = FakeExecutor(fail_forward={"submit"}, fail_compensation={"comp_create"})

    result = await _orchestrator().execute(_steps(), executor, saga_id="saga-5")

    assert result.status == SagaStatus.FAILED
    assert result.compensation_results[0].status == SagaStatus.FAILED
    assert "comp_create failed" in result.compensation_results[0].error


# ── 补偿来源与人工介入 ──


async def test_explicit_compensation_overrides_map() -> None:
    steps = [
        SagaStep(
            "s1",
            "create",
            {"bill": "SO1"},
            action="save",
            compensate_tool_name="manual_delete",
            compensate_arguments={"id": 1},
        ),
        SagaStep("s2", "submit", {"bill": "SO1"}, action="submit"),
    ]
    executor = FakeExecutor(fail_forward={"submit"})

    result = await _orchestrator().execute(steps, executor, saga_id="saga-6")

    assert result.status == SagaStatus.ROLLED_BACK
    assert executor.calls[-1] == ("manual_delete", {"id": 1})


async def test_missing_compensation_requires_manual_intervention() -> None:
    executor = FakeExecutor(fail_forward={"submit"})

    result = await _orchestrator(compensate_map={}).execute(_steps(), executor, saga_id="saga-7")

    assert result.status == SagaStatus.FAILED
    assert result.compensation_results[0].manual_intervention is True
    assert "未配置补偿规则" in result.compensation_results[0].error
    assert [name for name, _ in executor.calls] == ["create", "submit"]


async def test_no_auto_compensate_skips_execution() -> None:
    executor = FakeExecutor(fail_forward={"submit"})

    result = await _orchestrator(no_auto_compensate=frozenset({"create"})).execute(
        _steps(), executor, saga_id="saga-8"
    )

    assert result.status == SagaStatus.FAILED
    assert result.compensation_results[0].manual_intervention is True
    assert "不可自动补偿" in result.compensation_results[0].error
    assert [name for name, _ in executor.calls] == ["create", "submit"]


# ── 幂等与装配校验 ──


async def test_repeat_saga_reuses_success_and_compensation() -> None:
    idempotency = IdempotencyService(ttl_seconds=3600, clock=FakeClock())
    orchestrator = _orchestrator(idempotency=idempotency)
    executor = FakeExecutor(fail_forward={"submit"})

    first = await orchestrator.execute(_steps(), executor, saga_id="saga-repeat")
    second = await orchestrator.execute(_steps(), executor, saga_id="saga-repeat")

    assert first.status == SagaStatus.ROLLED_BACK
    assert second.status == SagaStatus.ROLLED_BACK
    # create 成功被缓存；submit 失败可重试；补偿成功被缓存
    assert [name for name, _ in executor.calls] == [
        "create",
        "submit",
        "comp_create",
        "submit",
    ]


async def test_empty_steps_and_duplicate_ids_rejected() -> None:
    with pytest.raises(ValueError, match="至少需要一个步骤"):
        await _orchestrator().execute([], FakeExecutor())

    duplicate = [
        SagaStep("s1", "create", {}),
        SagaStep("s1", "submit", {}),
    ]
    with pytest.raises(ValueError, match="重复"):
        await _orchestrator().execute(duplicate, FakeExecutor())


def test_step_requires_id_and_tool() -> None:
    with pytest.raises(ValueError, match="step_id"):
        SagaStep("", "create", {})
    with pytest.raises(ValueError, match="tool_name"):
        SagaStep("s1", "  ", {})


def test_result_is_json_serializable() -> None:
    result = SagaResult(
        saga_id="s1",
        trace_id="t1",
        status=SagaStatus.ROLLED_BACK,
        step_results=(SagaStepResult("s1", "create", SagaStatus.SUCCESS, result={"id": 1}),),
        compensation_results=(SagaStepResult("s1", "comp_create", SagaStatus.COMPENSATED),),
        status_history=(SagaStatus.PENDING, SagaStatus.RUNNING),
    )

    payload = json.loads(json.dumps(result.to_dict()))

    assert payload["status"] == "rolled_back"
    assert payload["step_results"][0]["result"] == {"id": 1}
    assert payload["compensation_results"][0]["status"] == "compensated"
