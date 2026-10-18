"""Saga 编排器（M5-T02，方案 §5.1 支柱三）。

职责：顺序执行多步写操作；任一步失败后**倒序补偿**已成功步骤；
状态机迁移显式记录日志；每一步与补偿都经过幂等层，避免重复写。

边界：
- 不做自动重试（失败即进入补偿；重试由上层/幂等层决定）
- 补偿参数是静态的（由调用方提前构造）；不自动注入前一步结果
- 无补偿映射 / 不可逆操作 → 标记人工介入，事务终态 FAILED
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from loguru import logger

from config.compensate_map import COMPENSATE_MAP, NO_AUTO_COMPENSATE
from kingdee.idempotency import IdempotencyService
from models.base import SagaStatus


class ToolExecutor(Protocol):
    """写工具执行协议（真实写接口或测试替身均实现它）。"""

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        """执行一步写操作并返回结果；失败抛异常。"""
        ...


@dataclass(frozen=True)
class SagaStep:
    """一个可执行且可补偿的步骤。"""

    step_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    action: str = ""
    # 显式补偿优先于 COMPENSATE_MAP；为空则查映射表
    compensate_tool_name: str = ""
    compensate_arguments: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValueError("SagaStep.step_id 不能为空")
        if not self.tool_name.strip():
            raise ValueError("SagaStep.tool_name 不能为空")


@dataclass(frozen=True)
class SagaStepResult:
    """单步执行/补偿结果。"""

    step_id: str
    tool_name: str
    status: SagaStatus
    result: Any = None
    error: str = ""
    manual_intervention: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "manual_intervention": self.manual_intervention,
        }


@dataclass(frozen=True)
class SagaResult:
    """一次 Saga 事务的最终结论。"""

    saga_id: str
    trace_id: str
    status: SagaStatus
    step_results: tuple[SagaStepResult, ...]
    compensation_results: tuple[SagaStepResult, ...] = ()
    error: str = ""
    status_history: tuple[SagaStatus, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的审计字典。"""
        return {
            "saga_id": self.saga_id,
            "trace_id": self.trace_id,
            "status": self.status.value,
            "step_results": [item.to_dict() for item in self.step_results],
            "compensation_results": [item.to_dict() for item in self.compensation_results],
            "error": self.error,
            "status_history": [status.value for status in self.status_history],
        }


class SagaOrchestrator:
    """顺序执行 + 失败倒序补偿的事务编排器。"""

    def __init__(
        self,
        *,
        idempotency: IdempotencyService | None = None,
        compensate_map: Mapping[tuple[str, str], str] | None = None,
        no_auto_compensate: frozenset[str] | None = None,
    ) -> None:
        self._idempotency = idempotency or IdempotencyService()
        self._compensate_map = (
            dict(compensate_map) if compensate_map is not None else dict(COMPENSATE_MAP)
        )
        self._no_auto = no_auto_compensate if no_auto_compensate is not None else NO_AUTO_COMPENSATE

    async def execute(
        self,
        steps: Sequence[SagaStep],
        executor: ToolExecutor,
        *,
        saga_id: str | None = None,
        trace_id: str = "",
    ) -> SagaResult:
        """执行事务；任一步失败即倒序补偿已成功步骤。

        Raises:
            ValueError: 空步骤或重复 step_id（装配期错误尽早暴露）。
        """
        if not steps:
            raise ValueError("Saga 至少需要一个步骤（fail-closed）")
        _validate_step_ids(steps)

        resolved_id = saga_id or uuid4().hex
        history: list[SagaStatus] = [SagaStatus.PENDING]
        _transition(history, resolved_id, SagaStatus.RUNNING)

        step_results: list[SagaStepResult] = []
        for step in steps:

            async def forward(step: SagaStep = step) -> Any:
                return await executor.execute(step.tool_name, step.arguments)

            try:
                result = await self._idempotency.run(
                    _forward_key(resolved_id, step.step_id), forward
                )
            except Exception as exc:  # 单步失败 → 进入补偿（fail-closed）
                step_results.append(
                    SagaStepResult(
                        step_id=step.step_id,
                        tool_name=step.tool_name,
                        status=SagaStatus.FAILED,
                        error=str(exc),
                    )
                )
                _transition(history, resolved_id, SagaStatus.FAILED, step.step_id)
                return await self._rollback(
                    resolved_id,
                    trace_id,
                    steps,
                    step_results,
                    executor,
                    history,
                    error=str(exc),
                )
            step_results.append(
                SagaStepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status=SagaStatus.SUCCESS,
                    result=result,
                )
            )

        _transition(history, resolved_id, SagaStatus.SUCCESS)
        return SagaResult(
            saga_id=resolved_id,
            trace_id=trace_id,
            status=SagaStatus.SUCCESS,
            step_results=tuple(step_results),
            status_history=tuple(history),
        )

    async def _rollback(
        self,
        saga_id: str,
        trace_id: str,
        steps: Sequence[SagaStep],
        step_results: list[SagaStepResult],
        executor: ToolExecutor,
        history: list[SagaStatus],
        *,
        error: str,
    ) -> SagaResult:
        """倒序补偿已成功步骤；无已成功步骤则直接 FAILED。"""
        successful_ids = {
            result.step_id for result in step_results if result.status == SagaStatus.SUCCESS
        }
        if not successful_ids:
            return SagaResult(
                saga_id=saga_id,
                trace_id=trace_id,
                status=SagaStatus.FAILED,
                step_results=tuple(step_results),
                error=error,
                status_history=tuple(history),
            )

        _transition(history, saga_id, SagaStatus.COMPENSATING)
        compensation_results: list[SagaStepResult] = []
        all_ok = True
        for step in reversed(steps):
            if step.step_id not in successful_ids:
                continue
            compensation, ok = await self._compensate_step(saga_id, step, executor)
            compensation_results.append(compensation)
            all_ok = all_ok and ok

        final_status = SagaStatus.ROLLED_BACK if all_ok else SagaStatus.FAILED
        _transition(history, saga_id, final_status)
        return SagaResult(
            saga_id=saga_id,
            trace_id=trace_id,
            status=final_status,
            step_results=tuple(step_results),
            compensation_results=tuple(compensation_results),
            error="" if all_ok else error,
            status_history=tuple(history),
        )

    async def _compensate_step(
        self, saga_id: str, step: SagaStep, executor: ToolExecutor
    ) -> tuple[SagaStepResult, bool]:
        """执行单步补偿；返回 (结果, 是否成功)。"""
        if step.tool_name in self._no_auto:
            return (
                SagaStepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status=SagaStatus.FAILED,
                    error="该操作不可自动补偿，需人工介入",
                    manual_intervention=True,
                ),
                False,
            )

        compensate_tool = step.compensate_tool_name or self._compensate_map.get(
            (step.tool_name, step.action), ""
        )
        if not compensate_tool:
            return (
                SagaStepResult(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    status=SagaStatus.FAILED,
                    error="未配置补偿规则，需人工介入",
                    manual_intervention=True,
                ),
                False,
            )

        arguments = (
            step.compensate_arguments if step.compensate_arguments is not None else step.arguments
        )

        async def compensate() -> Any:
            return await executor.execute(compensate_tool, arguments)

        try:
            result = await self._idempotency.run(_compensate_key(saga_id, step.step_id), compensate)
        except Exception as exc:  # 补偿失败 → 事务 FAILED，人工处理残留
            return (
                SagaStepResult(
                    step_id=step.step_id,
                    tool_name=compensate_tool,
                    status=SagaStatus.FAILED,
                    error=str(exc),
                ),
                False,
            )
        return (
            SagaStepResult(
                step_id=step.step_id,
                tool_name=compensate_tool,
                status=SagaStatus.COMPENSATED,
                result=result,
            ),
            True,
        )


def _validate_step_ids(steps: Sequence[SagaStep]) -> None:
    step_ids = [step.step_id for step in steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("SagaStep.step_id 重复（fail-closed）")


def _forward_key(saga_id: str, step_id: str) -> str:
    return f"{saga_id}:{step_id}"


def _compensate_key(saga_id: str, step_id: str) -> str:
    return f"{saga_id}:{step_id}:compensate"


def _transition(
    history: list[SagaStatus],
    saga_id: str,
    new_status: SagaStatus,
    step_id: str = "",
) -> None:
    """记录状态迁移（显式日志 + 历史序列）。"""
    previous = history[-1]
    logger.info(
        "saga.transition | saga_id={} from={} to={} step={}",
        saga_id,
        previous.value,
        new_status.value,
        step_id or "-",
    )
    history.append(new_status)
