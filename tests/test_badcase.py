"""M9-T07：Badcase 服务层测试（脱敏 / 去重 / 导出 / 幂等 / 审计）。"""

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from agent.tools import build_default_registry
from governance.badcase import BadcaseService, FileBadcaseStore
from governance.evaluation.dataset import load_dataset
from infrastructure.exceptions import BadcaseError
from models.agent import AgentTrace
from models.audit import AuditEvent
from models.user import UserContext
from observability.audit import AuditLogger
from observability.tracing import TraceStore
from safety.dlp import DlpFinding, DlpScanner, DlpScanResult

_REPO_DATASET = (
    Path(__file__).resolve().parents[1] / "evals" / "datasets" / "v1" / "tool_selection.json"
)
_ADMIN = UserContext(user_id="admin", role="admin")
_KNOWN_TOOLS = frozenset(spec.name for spec in build_default_registry().all_specs())


class _MemorySink:
    """内存审计出口（断言事件内容用）。"""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _BlockingScanner(DlpScanner):
    """模拟脱敏后仍命中的 DLP（验证 fail-closed）。"""

    def scan(self, text: str) -> DlpScanResult:
        return DlpScanResult(findings=(DlpFinding(category="phone", count=1),))


def _save_trace(store: TraceStore, question: str, *, user_id: str = "alice") -> str:
    trace = AgentTrace(
        trace_id=uuid4(),
        user_input=question,
        user_context=UserContext(user_id=user_id, role="admin"),
        user_id=user_id,
    )
    store.save(trace)
    return str(trace.trace_id)


def _service(
    tmp_path: Path,
    trace_store: TraceStore,
    *,
    audit: AuditLogger | None = None,
    scanner: DlpScanner | None = None,
) -> BadcaseService:
    return BadcaseService(
        FileBadcaseStore(tmp_path / "badcases"),
        trace_store=trace_store,
        tools=build_default_registry(),
        dataset_root=tmp_path / "eval_datasets",
        audit=audit,
        scanner=scanner,
    )


async def test_mark_extracts_question_and_masks_pii(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "客户手机 13800138000 的订单")
    service = _service(tmp_path, traces)

    record = await service.mark(
        trace_id=trace_id,
        expected_tool="erp_query_customer",
        reason="手机 13800138000 应查客户却查了订单",
        category="badcase",
        expected_behavior="联系 13800138000 后再查",
        user=_ADMIN,
    )

    assert record.status == "marked"
    assert "13800138000" not in record.question
    assert "138****8000" in record.question
    assert "13800138000" not in record.reason
    assert len(list((tmp_path / "badcases").glob("*.json"))) == 1


async def test_same_trace_is_idempotent(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "查一下客户 CUS001")
    service = _service(tmp_path, traces)

    first = await service.mark(
        trace_id=trace_id,
        expected_tool="erp_query_customer",
        reason="选错工具",
        category="badcase",
        expected_behavior="",
        user=_ADMIN,
    )
    second = await service.mark(
        trace_id=trace_id,
        expected_tool="erp_query_sales_order",
        reason="再次标记",
        category="badcase",
        expected_behavior="",
        user=_ADMIN,
    )

    assert first.badcase_id == second.badcase_id
    assert len(list((tmp_path / "badcases").glob("*.json"))) == 1


async def test_unknown_tool_and_invalid_reason_are_rejected(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "查一下客户 CUS001")
    service = _service(tmp_path, traces)

    with pytest.raises(BadcaseError) as unknown:
        await service.mark(
            trace_id=trace_id,
            expected_tool="kingdee_drop_database",
            reason="选错工具",
            category="badcase",
            expected_behavior="",
            user=_ADMIN,
        )
    assert unknown.value.code == "badcase_unknown_tool"

    with pytest.raises(BadcaseError) as too_long:
        await service.mark(
            trace_id=trace_id,
            expected_tool=None,
            reason="x" * 501,
            category="badcase",
            expected_behavior="",
            user=_ADMIN,
        )
    assert too_long.value.code == "badcase_invalid"
    assert not list((tmp_path / "badcases").glob("*.json"))


async def test_dlp_hit_is_rejected_without_writing(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "查一下客户 CUS001")
    service = _service(tmp_path, traces, scanner=_BlockingScanner())

    with pytest.raises(BadcaseError) as blocked:
        await service.mark(
            trace_id=trace_id,
            expected_tool="erp_query_customer",
            reason="选错工具",
            category="badcase",
            expected_behavior="",
            user=_ADMIN,
        )
    assert blocked.value.code == "badcase_sensitive"
    assert not list((tmp_path / "badcases").glob("*.json"))


async def test_export_appends_to_existing_dataset_and_is_idempotent(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "请帮我看看客户 CUS001 最近下的销售订单")
    service = _service(tmp_path, traces)

    dataset_dir = tmp_path / "eval_datasets" / "v1"
    dataset_dir.mkdir(parents=True)
    shutil.copy2(_REPO_DATASET, dataset_dir / "tool_selection.json")
    before = load_dataset(dataset_dir / "tool_selection.json", known_tools=_KNOWN_TOOLS)

    record = await service.mark(
        trace_id=trace_id,
        expected_tool="erp_query_sales_order",
        reason="选错工具",
        category="badcase",
        expected_behavior="先按客户编码过滤",
        user=_ADMIN,
    )
    exported = await service.export(record.badcase_id, dataset_version="v1", user=_ADMIN)

    dataset = load_dataset(dataset_dir / "tool_selection.json", known_tools=_KNOWN_TOOLS)
    assert exported.status == "exported"
    assert exported.exported_question_id.startswith("BC-")
    assert len(dataset.questions) == len(before.questions) + 1
    assert {question.question_id for question in dataset.questions} >= {
        question.question_id for question in before.questions
    }
    exported_case = next(
        question
        for question in dataset.questions
        if question.question_id == exported.exported_question_id
    )
    assert exported_case.expected_tool == "erp_query_sales_order"

    again = await service.export(record.badcase_id, dataset_version="v1", user=_ADMIN)
    assert again.exported_question_id == exported.exported_question_id
    assert (
        len(load_dataset(dataset_dir / "tool_selection.json").questions)
        == len(before.questions) + 1
    )


async def test_duplicate_question_and_tool_is_rejected(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    first_trace = _save_trace(traces, "查一下客户 CUS001")
    second_trace = _save_trace(traces, "查一下客户 CUS001", user_id="bob")
    service = _service(tmp_path, traces)

    first = await service.mark(
        trace_id=first_trace,
        expected_tool="erp_query_customer",
        reason="选错工具",
        category="badcase",
        expected_behavior="",
        user=_ADMIN,
    )
    second = await service.mark(
        trace_id=second_trace,
        expected_tool="erp_query_customer",
        reason="同样的问题",
        category="badcase",
        expected_behavior="",
        user=_ADMIN,
    )
    await service.export(first.badcase_id, user=_ADMIN)

    with pytest.raises(BadcaseError) as duplicate:
        await service.export(second.badcase_id, user=_ADMIN)
    assert duplicate.value.code == "badcase_duplicate"


async def test_corrupt_dataset_fails_closed(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "查一下客户 CUS001")
    service = _service(tmp_path, traces)
    dataset_dir = tmp_path / "eval_datasets" / "v1"
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "tool_selection.json").write_text("{broken", encoding="utf-8")

    record = await service.mark(
        trace_id=trace_id,
        expected_tool="erp_query_customer",
        reason="选错工具",
        category="badcase",
        expected_behavior="",
        user=_ADMIN,
    )

    with pytest.raises(BadcaseError) as conflict:
        await service.export(record.badcase_id, user=_ADMIN)
    assert conflict.value.code == "badcase_dataset_conflict"


async def test_corrupt_badcase_file_is_skipped_in_list(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "查一下客户 CUS001")
    service = _service(tmp_path, traces)
    record = await service.mark(
        trace_id=trace_id,
        expected_tool="erp_query_customer",
        reason="选错工具",
        category="badcase",
        expected_behavior="",
        user=_ADMIN,
    )
    (tmp_path / "badcases" / "broken.json").write_text("{}", encoding="utf-8")

    records = await service.list_records(_ADMIN)

    assert [item.badcase_id for item in records] == [record.badcase_id]


async def test_audit_detail_does_not_contain_question_text(tmp_path: Path) -> None:
    sink = _MemorySink()
    audit = AuditLogger(sink)
    traces = TraceStore(tmp_path / "traces")
    trace_id = _save_trace(traces, "查一下客户 CUS001 的订单")
    service = _service(tmp_path, traces, audit=audit)
    record = await service.mark(
        trace_id=trace_id,
        expected_tool="erp_query_customer",
        reason="应查客户却查了订单",
        category="badcase",
        expected_behavior="先换客户编码",
        user=_ADMIN,
    )
    await service.export(record.badcase_id, user=_ADMIN)

    payload = json.dumps([event.to_dict() for event in sink.events], ensure_ascii=False)
    assert [event.event_type for event in sink.events] == ["badcase.marked", "badcase.exported"]
    assert record.question not in payload
    assert "应查客户却查了订单" not in payload
    assert record.badcase_id in payload


async def test_viewer_cannot_operate(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path / "traces")
    service = _service(tmp_path, traces)
    viewer = UserContext(user_id="viewer", role="viewer")

    with pytest.raises(BadcaseError) as forbidden:
        await service.list_records(viewer)
    assert forbidden.value.code == "forbidden"
