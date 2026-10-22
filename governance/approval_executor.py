"""审批通过后的恢复执行（M9-T05）。

执行链：
    ApprovalService.get_for_execution
    → 账本恢复检查
    → begin_execution（running）
    → TraceStore 加载原 trace（归属校验）
    → WriteCoordinator 单步 Saga
    → finish_execution（succeeded/failed）

边界：
- 单写调用；form_id/business_key 必须非空
- 默认 RegistryToolExecutor 只适用于未来统一 execute 的写工具
- 默认 NoopSnapshotProvider；edit/delete 需要真实快照时由 WriteCoordinator fail-closed
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol
from weakref import WeakValueDictionary

from loguru import logger

from agent.tools import ToolRegistry
from erp.protocol import ErpClient
from governance.approval import ApprovalService
from infrastructure.exceptions import ApprovalError, TraceStoreError
from kingdee.saga import SagaStep, ToolExecutor
from kingdee.write_coordinator import (
    SnapshotProvider,
    WriteCoordinator,
    WriteOperation,
)
from ledger.write_ledger import WriteLedger
from models.approval import PendingAction
from models.base import SagaStatus
from observability.tracing import TraceStore


class ApprovalExecution(Protocol):
    """审批执行接口（API 只依赖本协议，测试可注入替身）。"""

    async def execute(self, approval_id: str) -> PendingAction:
        """恢复执行一张 approved 审批单。"""
        ...


class RegistryToolExecutor:
    """把工具名解析到 ToolRegistry 并执行（未来写工具的统一入口）。"""

    def __init__(self, tools: ToolRegistry, client: ErpClient) -> None:
        self._tools = tools
        self._client = client

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        tool = self._tools.get(tool_name)
        return await tool.execute(self._client, dict(arguments))


class NoopSnapshotProvider:
    """无真实写接口时的快照占位：返回 None，由 WriteCoordinator 决定是否允许。"""

    async def before(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        return None

    async def after(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        return None


class ApprovalExecutor:
    """审批单恢复执行器（进程内按 approval_id 串行）。"""

    def __init__(
        self,
        approvals: ApprovalService,
        *,
        trace_store: TraceStore,
        writer: WriteCoordinator,
        executor: ToolExecutor,
        snapshots: SnapshotProvider,
        ledger: WriteLedger,
    ) -> None:
        self._approvals = approvals
        self._trace_store = trace_store
        self._writer = writer
        self._executor = executor
        self._snapshots = snapshots
        self._ledger = ledger
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def execute(self, approval_id: str) -> PendingAction:
        """执行审批动作；成功/失败都落 execution_status，不重复写。"""
        async with self._lock_for(approval_id):
            action = await self._approvals.get_for_execution(approval_id)
            if action.status != "approved":
                raise ApprovalError("审批单未通过，不能执行", code="approval_conflict")
            if action.execution_status == "succeeded":
                return action

            # 上次执行可能已写成功但未来得及落 execution_status
            if self._ledger_has_success(action):
                return await self._approvals.finish_execution(
                    approval_id,
                    succeeded=True,
                    result={"recovered_from_ledger": True},
                    error="",
                )

            action = await self._approvals.begin_execution(approval_id)
            try:
                trace = self._trace_store.load(action.trace_id)
            except TraceStoreError:
                return await self._approvals.finish_execution(
                    approval_id,
                    succeeded=False,
                    result={},
                    error="Trace 不存在，无法恢复执行",
                )
            if trace.user_id != action.requester_id:
                return await self._approvals.finish_execution(
                    approval_id,
                    succeeded=False,
                    result={},
                    error="Trace 归属不符，拒绝执行",
                )

            try:
                operation = self._operation(action)
                result = await self._writer.execute(
                    trace,
                    [operation],
                    self._executor,
                    self._snapshots,
                    saga_id=action.approval_id,
                )
            except Exception as exc:
                logger.exception("审批执行异常 | approval_id={}", approval_id)
                return await self._approvals.finish_execution(
                    approval_id,
                    succeeded=False,
                    result={},
                    error=f"执行异常: {type(exc).__name__}",
                )

            success = result.saga.status == SagaStatus.SUCCESS
            return await self._approvals.finish_execution(
                approval_id,
                succeeded=success,
                result={
                    "saga_id": result.saga.saga_id,
                    "status": result.saga.status.value,
                    "steps": len(result.saga.step_results),
                    "dead_letter": result.dead_letter is not None,
                },
                error="" if success else (result.saga.error or "Saga 失败"),
            )

    def _operation(self, action: PendingAction) -> WriteOperation:
        if not action.form_id or not action.business_key:
            raise ValueError("审批单缺少 form_id/business_key，拒绝执行")
        return WriteOperation(
            SagaStep(
                step_id=action.approval_id,
                tool_name=action.tool_name,
                arguments=action.arguments,
                action=action.write_action,
            ),
            form_id=action.form_id,
            business_key=action.business_key,
        )

    def _ledger_has_success(self, action: PendingAction) -> bool:
        for record in self._ledger.find_by_trace_id(action.trace_id):
            if (
                record.status == "success"
                and record.form_id == action.form_id
                and record.business_key == action.business_key
                and record.action == action.write_action
            ):
                return True
        return False

    def _lock_for(self, approval_id: str) -> asyncio.Lock:
        lock = self._locks.get(approval_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[approval_id] = lock
        return lock
