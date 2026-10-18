"""写操作协调器（M5-T06）：幂等 + Saga + 快照 + 账本 + 证据链。

一次多步写事务：
    每个操作：before 快照 → 执行写工具 → after 快照 → 落账（成功/失败）
    任一步失败：Saga 倒序补偿已成功步骤；失败步骤也会落账

边界：
- 快照由 ``SnapshotProvider`` 注入（M5 无真实金蝶写接口）
- 补偿结果记录在 ``SagaResult.compensation_results``，不单独落账
- 本模块暂不接入 AgentCore Phase 3（无真实写工具）；它是未来接入点
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import uuid4

from loguru import logger

from infrastructure.exceptions import SnapshotError
from kingdee.idempotency import IdempotencyService
from kingdee.saga import SagaOrchestrator, SagaResult, SagaStep, ToolExecutor
from ledger.provenance import build_provenance
from ledger.write_ledger import WriteLedger
from models.agent import AgentTrace
from models.dead_letter import DeadLetter
from models.ledger import WriteAction, WriteRecord, WriteStatus
from models.llm import ToolCallDecision
from observability.dead_letter import DeadLetterService

_WRITE_ACTIONS = frozenset({"save", "edit", "submit", "audit", "unaudit", "delete"})
# 这些动作必须先查到原状态，否则就是“盲改”
_ACTIONS_REQUIRING_BEFORE = frozenset({"edit", "unaudit", "delete"})


class SnapshotProvider(Protocol):
    """写前/写后快照来源（真实实现查金蝶，测试用 fake）。"""

    async def before(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """写前快照；新建（save）返回 None 合法。"""
        ...

    async def after(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """写后快照；查询失败可返回 None。"""
        ...


@dataclass(frozen=True)
class WriteOperation:
    """一步写操作 + 账本所需业务元数据。"""

    step: SagaStep
    form_id: str
    business_key: str
    kingdee_doc_id: str = ""

    def __post_init__(self) -> None:
        if not self.form_id.strip():
            raise ValueError("WriteOperation.form_id 不能为空")
        if not self.business_key.strip():
            raise ValueError("WriteOperation.business_key 不能为空")


@dataclass(frozen=True)
class WriteExecutionResult:
    """多步写事务的最终结果（Saga 结论 + 落账记录）。"""

    saga: SagaResult
    records: tuple[WriteRecord, ...]
    dead_letter: DeadLetter | None = None


class WriteCoordinator:
    """组合 Saga / 幂等 / 快照 / 账本 / 证据链。"""

    def __init__(
        self,
        ledger: WriteLedger,
        *,
        idempotency: IdempotencyService | None = None,
        compensate_map: Mapping[tuple[str, str], str] | None = None,
        no_auto_compensate: frozenset[str] | None = None,
        dead_letters: DeadLetterService | None = None,
    ) -> None:
        self._ledger = ledger
        self._dead_letters = dead_letters
        self._saga = SagaOrchestrator(
            idempotency=idempotency,
            compensate_map=compensate_map,
            no_auto_compensate=no_auto_compensate,
        )

    async def execute(
        self,
        trace: AgentTrace,
        operations: Sequence[WriteOperation],
        executor: ToolExecutor,
        snapshots: SnapshotProvider,
        *,
        saga_id: str | None = None,
    ) -> WriteExecutionResult:
        """执行多步写事务；任一步失败即倒序补偿并落账。"""
        if not operations:
            raise ValueError("写事务至少需要一个操作（fail-closed）")
        _validate_operations(operations)

        resolved_saga_id = saga_id or uuid4().hex
        ledgered = _LedgeredExecutor(
            trace=trace,
            saga_id=resolved_saga_id,
            operations={op.step.tool_name: op for op in operations},
            executor=executor,
            snapshots=snapshots,
            ledger=self._ledger,
        )
        saga_result = await self._saga.execute(
            [op.step for op in operations],
            ledgered,
            saga_id=resolved_saga_id,
            trace_id=str(trace.trace_id),
        )

        dead_letter = None
        if self._dead_letters is not None:
            dead_letter = self._dead_letters.enqueue_from_saga(
                saga_result,
                trace_id=str(trace.trace_id),
                request={
                    "operations": [
                        {
                            "tool_name": op.step.tool_name,
                            "arguments": dict(op.step.arguments),
                            "form_id": op.form_id,
                            "business_key": op.business_key,
                        }
                        for op in operations
                    ]
                },
            )

        return WriteExecutionResult(
            saga=saga_result,
            records=tuple(ledgered.records),
            dead_letter=dead_letter,
        )


class _LedgeredExecutor:
    """包装真实 executor：在每步写前后取快照并落账。"""

    def __init__(
        self,
        *,
        trace: AgentTrace,
        saga_id: str,
        operations: Mapping[str, WriteOperation],
        executor: ToolExecutor,
        snapshots: SnapshotProvider,
        ledger: WriteLedger,
    ) -> None:
        self._trace = trace
        self._saga_id = saga_id
        self._operations = dict(operations)
        self._executor = executor
        self._snapshots = snapshots
        self._ledger = ledger
        self.records: list[WriteRecord] = []

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        operation = self._operations.get(tool_name)
        if operation is None:
            # 补偿工具：执行但不单独落账（结果在 SagaResult.compensation_results）
            return await self._executor.execute(tool_name, arguments)

        action = _validate_action(operation.step.action)
        before = await self._read_before(tool_name, arguments, action)
        api_request = {"tool_name": tool_name, "arguments": dict(arguments)}

        try:
            response = await self._executor.execute(tool_name, arguments)
        except Exception as exc:
            self._append(
                operation,
                action,
                before,
                None,
                status="failed",
                api_request=api_request,
                api_response={"error": str(exc)},
            )
            raise

        after = await self._read_after(tool_name, arguments)
        self._append(
            operation,
            action,
            before,
            after,
            status="success",
            api_request=api_request,
            api_response=_as_mapping(response),
        )
        return response

    async def _read_before(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        action: WriteAction,
    ) -> dict[str, Any] | None:
        try:
            before = await self._snapshots.before(tool_name, arguments)
        except Exception as exc:
            if action in _ACTIONS_REQUIRING_BEFORE:
                raise SnapshotError(
                    f"写前快照失败，中止 {action} 操作: {tool_name}",
                    context={"tool": tool_name, "action": action},
                ) from exc
            logger.warning("写前快照失败，按新建继续 | tool={} error={}", tool_name, exc)
            return None

        if action in _ACTIONS_REQUIRING_BEFORE and before is None:
            raise SnapshotError(
                f"写前快照为空，中止 {action} 操作: {tool_name}",
                context={"tool": tool_name, "action": action},
            )
        return before

    async def _read_after(
        self, tool_name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        try:
            return await self._snapshots.after(tool_name, arguments)
        except Exception as exc:
            # 写入已发生；写后核验失败只告警，不影响落账
            logger.warning("写后快照失败 | tool={} error={}", tool_name, exc)
            return None

    def _append(
        self,
        operation: WriteOperation,
        action: WriteAction,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any] | None,
        *,
        status: WriteStatus,
        api_request: Mapping[str, Any],
        api_response: Mapping[str, Any],
    ) -> None:
        tool_call = ToolCallDecision(
            tool_name=operation.step.tool_name,
            arguments=dict(operation.step.arguments),
        )
        provenance = build_provenance(
            self._trace,
            tool_call,
            tool_call_params=operation.step.arguments,
            api_request=api_request,
            api_response=api_response,
        )
        record = self._ledger.record_write(
            trace_id=str(self._trace.trace_id),
            form_id=operation.form_id,
            business_key=operation.business_key,
            action=action,
            provenance=provenance,
            before_snapshot=before,
            after_snapshot=after,
            saga_id=self._saga_id,
            kingdee_doc_id=operation.kingdee_doc_id,
            status=status,
        )
        self.records.append(record)


def _validate_operations(operations: Sequence[WriteOperation]) -> None:
    tool_names = [op.step.tool_name for op in operations]
    if len(tool_names) != len(set(tool_names)):
        raise ValueError("同一写事务内 tool_name 不能重复（账本元数据按工具名映射）")


def _validate_action(action: str) -> WriteAction:
    resolved = action or "save"
    if resolved not in _WRITE_ACTIONS:
        raise ValueError(f"不支持的写动作: {action}")
    return cast(WriteAction, resolved)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    return {"result": value}
