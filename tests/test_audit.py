"""M6-T04：审计事件统一出口单测。

覆盖事件模型、JSONL append-only、订阅者隔离、写入失败不阻塞，
以及 M4 Guard/Permission 与 M5 账本的审计接线。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import Field

from agent.tools import ToolDefinition, ToolRegistry
from erp import FilterExpression
from kingdee.client import KingdeeClient
from kingdee.endpoints.base import EndpointDescriptor, EndpointParams, FieldDescriptor
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.audit import AuditEvent
from models.base import KingPilotBaseModel
from models.ledger import ProvenanceChain
from models.llm import ToolCallDecision, ToolSpec
from models.user import UserContext
from observability.audit import AuditLogger, JsonlAuditSink
from safety.engine import SafetyLayer
from safety.guard import GuardContext, GuardPipeline, GuardResult
from safety.permission_engine import PermissionEngine


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FailingSink:
    def write(self, event: AuditEvent) -> None:
        raise RuntimeError("disk full")


class _BlockingGuard:
    name = "guard_test"

    async def check(self, context: GuardContext) -> GuardResult:
        return GuardResult.block("测试拦截")


class FakeIO:
    async def ask(self, prompt: str) -> bool:
        return True


class _Params(EndpointParams):
    bill_no: str = Field(default="", description="单据编号")


def _empty_filter(params: KingPilotBaseModel) -> FilterExpression:
    return FilterExpression()


_FINANCE_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="erp_query_finance_vouchers",
        description="财务凭证查询",
        parameters=_Params.model_json_schema(),
    ),
    params_model=_Params,
    build_filter=_empty_filter,
    endpoint=EndpointDescriptor(
        form_id="GL_Voucher",
        label="会计凭证",
        fields=(
            FieldDescriptor(
                name="FBillNo", data_type="string", filterable=True, description="编号"
            ),
        ),
        default_field_keys=("FBillNo",),
        default_sort="FBillNo",
    ),
)


def _operator() -> UserContext:
    return UserContext(user_id="u001", role="operator", ip_address="10.0.0.1")


def _event(event_type: str = "guard.blocked") -> AuditEvent:
    return AuditEvent(
        event_type=event_type,
        trace_id="trace-1",
        user_id="u001",
        user_role="operator",
        tool_name="tool",
        status="blocked",
    )


# ── 模型与出口 ──


def test_event_defaults_and_json() -> None:
    event = _event()
    payload = json.loads(json.dumps(event.to_dict()))

    assert len(event.event_id) == 32
    assert isinstance(event.timestamp, datetime)
    assert payload["event_type"] == "guard.blocked"
    assert payload["status"] == "blocked"


def test_jsonl_sink_appends_and_creates_parent(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "events.jsonl"
    logger = AuditLogger(JsonlAuditSink(path))

    logger.emit(_event())
    logger.emit(_event("permission.denied"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "guard.blocked"
    assert json.loads(lines[1])["event_type"] == "permission.denied"


def test_subscribers_are_isolated() -> None:
    received: list[AuditEvent] = []
    logger = AuditLogger(RecordingSink())

    def _raise(event: AuditEvent) -> None:
        raise RuntimeError("observer boom")

    logger.subscribe(_raise)
    logger.subscribe(received.append)
    logger.emit(_event())

    assert len(received) == 1


def test_sink_failure_does_not_raise() -> None:
    logger = AuditLogger(_FailingSink())

    logger.emit(_event())  # 不抛异常即可


# ── M4 接线 ──


async def test_guard_pipeline_emits_guard_blocked() -> None:
    sink = RecordingSink()
    pipeline = GuardPipeline([_BlockingGuard()], audit=AuditLogger(sink))

    result = await pipeline.check(
        GuardContext(
            tool_name="tool",
            arguments={},
            user_context=_operator(),
            trace_id="trace-1",
        )
    )

    assert result.blocked is True
    event = sink.events[0]
    assert event.event_type == "guard.blocked"
    assert event.trace_id == "trace-1"
    assert event.user_id == "u001"
    assert event.user_role == "operator"
    assert event.tool_name == "tool"
    assert event.status == "blocked"
    assert event.detail["guard"] == "guard_test"
    assert event.detail["reason"] == "测试拦截"


def test_permission_engine_emits_denied_event() -> None:
    sink = RecordingSink()
    engine = PermissionEngine(audit=AuditLogger(sink))

    decision = engine.check(_operator(), _FINANCE_TOOL, {}, trace_id="trace-1")

    assert decision.allowed is False
    event = sink.events[0]
    assert event.event_type == "permission.denied"
    assert event.trace_id == "trace-1"
    assert event.form_id == "GL_Voucher"
    assert event.user_role == "operator"
    assert event.detail["domain"] == "finance"
    assert event.status == "rejected"


# ── M5 接线 ──


def test_write_ledger_emits_ledger_event(tmp_path: Path) -> None:
    sink = RecordingSink()
    store = SqliteWriteLedgerStore(tmp_path / "ledger.db")
    ledger = WriteLedger(store, audit=AuditLogger(sink))
    provenance = ProvenanceChain(user_id="u001", user_original_input="改金额")

    record = ledger.record_write(
        trace_id="trace-1",
        form_id="SAL_Order",
        business_key="SO1",
        action="edit",
        provenance=provenance,
        before_snapshot={"FAmount": 100},
        after_snapshot={"FAmount": 80},
        saga_id="saga-1",
    )

    event = sink.events[0]
    assert event.event_type == "ledger.write"
    assert event.trace_id == "trace-1"
    assert event.business_key == "SO1"
    assert event.action == "edit"
    assert event.status == "success"
    assert event.saga_id == "saga-1"
    assert event.detail["record_id"] == record.record_id
    assert event.detail["record_hash"] == record.record_hash
    store.close()


# ── 集成 ──


async def test_safety_layer_emits_permission_event() -> None:
    sink = RecordingSink()
    audit = AuditLogger(sink)
    registry = ToolRegistry()
    registry.register(_FINANCE_TOOL)
    client = cast(KingdeeClient, object())
    layer = SafetyLayer(registry, client, FakeIO(), audit=audit)

    verdict = await layer.review(
        _operator(),
        [ToolCallDecision(tool_name=_FINANCE_TOOL.name, arguments={})],
        "trace-1",
    )

    assert verdict.blocked is True
    assert any(event.event_type == "permission.denied" for event in sink.events)
