"""M9-T13：写工具 + 真沙箱 + 快照/账本链路（集成测试）。"""

from pathlib import Path

from agent.tools import build_default_registry
from governance.approval_executor import RegistryToolExecutor
from kingdee.client import KingdeeClient
from kingdee.idempotency import IdempotencyService
from kingdee.saga import SagaStep
from kingdee.snapshot import KingdeeSnapshotProvider
from kingdee.write_coordinator import WriteCoordinator, WriteOperation
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.agent import AgentTrace
from models.base import SagaStatus
from models.user import UserContext

_CREATE = "erp_create_sales_order"
_SUBMIT = "erp_submit_sales_order"


async def test_create_then_submit_with_snapshots_and_ledger(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    registry = build_default_registry()
    ledger = WriteLedger(SqliteWriteLedgerStore(tmp_path / "ledger.sqlite3"))
    writer = WriteCoordinator(ledger, idempotency=IdempotencyService())
    executor = RegistryToolExecutor(registry, client)
    snapshots = KingdeeSnapshotProvider(client)
    trace = AgentTrace(
        user_input="创建并提交销售订单",
        user_context=UserContext(user_id="alice", role="salesperson"),
        user_id="alice",
    )

    create = WriteOperation(
        SagaStep(
            step_id="step-create",
            tool_name=_CREATE,
            # Guard 3 换码后的参数（FID）
            arguments={
                "customer_number": "2002",
                "material_number": "3003",
                "quantity": 2,
                "tax_price": 50,
            },
            action="save",
        ),
        form_id="SAL_Order",
        business_key="CREATE-integration",
    )
    created = await writer.execute(trace, [create], executor, snapshots)
    assert created.saga.status == SagaStatus.SUCCESS
    bill_no = str(created.records[0].provenance.api_response["bill_no"])
    assert bill_no == "SO900001"
    assert created.records[0].after_snapshot is None

    submit = WriteOperation(
        SagaStep(
            step_id="step-submit",
            tool_name=_SUBMIT,
            arguments={"bill_no": bill_no},
            action="submit",
        ),
        form_id="SAL_Order",
        business_key=bill_no,
    )
    submitted = await writer.execute(trace, [submit], executor, snapshots)
    assert submitted.saga.status == SagaStatus.SUCCESS
    record = submitted.records[0]
    assert record.business_key == bill_no
    assert record.before_snapshot is not None
    assert record.after_snapshot is not None
    assert record.before_snapshot["FDocumentStatus"] == "Z"
    assert record.after_snapshot["FDocumentStatus"] == "B"
    assert record.action == "submit"

    assert ledger.verify().valid is True
    assert len(ledger.records()) == 2
