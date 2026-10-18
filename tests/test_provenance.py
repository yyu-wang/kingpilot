"""M5-T05：证据链构建与报告单测。

覆盖从 AgentTrace 构建、参数/API 覆盖、缺省降级、报告渲染与按 trace/单据查询。
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from ledger.hash_chain import GENESIS_HASH
from ledger.provenance import (
    ProvenanceReporter,
    build_provenance,
    render_provenance_report,
    render_trace_report,
)
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.agent import AgentTrace
from models.ledger import FieldDiff, ProvenanceChain, WriteRecord
from models.llm import ToolCallDecision
from models.user import UserContext


def _trace(**overrides: object) -> AgentTrace:
    payload: dict[str, object] = {
        "user_input": "把 SO1 金额改成 8 万",
        "user_context": UserContext(user_id="u001", role="operator"),
        "user_id": "u001",
        "session_id": "s1",
        "llm_planning": {"reasoning": "修改 SO1 金额为 80000"},
        "guard_checks": [
            {"guard": "guard_permission", "passed": True, "reason": ""},
            {"guard": "guard_schema", "passed": True, "reason": ""},
        ],
        "risk_assessment": {
            "score": 42.0,
            "level": "confirm",
            "hitl": {"approved": True, "level": "confirm"},
        },
        "permission_check": {"allowed": True, "required_approvers": ["sales_manager"]},
    }
    payload.update(overrides)
    return AgentTrace.model_validate(payload)


_CALL = ToolCallDecision(
    tool_name="edit_sales_order",
    arguments={"bill_no": "SO1", "amount": 80000},
)


def _record(chain: ProvenanceChain, **overrides: object) -> WriteRecord:
    payload: dict[str, object] = {
        "record_id": "r1",
        "trace_id": "trace-1",
        "timestamp": datetime(2026, 10, 18, 10, 0),
        "form_id": "SAL_Order",
        "business_key": "SO1",
        "action": "edit",
        "before_snapshot": {"FAmount": 100},
        "after_snapshot": {"FAmount": 80},
        "field_diff": [FieldDiff(field_name="FAmount", old_value=100, new_value=80)],
        "provenance": chain,
        "prev_record_hash": GENESIS_HASH,
        "record_hash": "a" * 64,
    }
    payload.update(overrides)
    return WriteRecord.model_validate(payload)


# ── 构建证据链 ──


def test_build_provenance_from_trace() -> None:
    chain = build_provenance(
        _trace(),
        _CALL,
        tool_call_params={"bill_no": "SO1", "amount": 80000, "customer_number": "100001"},
        api_request={"FormId": "SAL_Order"},
        api_response={"Result": {"ResponseStatus": {"IsSuccess": True}}},
    )

    assert chain.user_original_input == "把 SO1 金额改成 8 万"
    assert chain.user_id == "u001"
    assert chain.session_id == "s1"
    assert chain.llm_reasoning == "修改 SO1 金额为 80000"
    assert chain.tool_call_params == {
        "bill_no": "SO1",
        "amount": 80000,
        "customer_number": "100001",
    }
    assert len(chain.guard_check_results) == 2
    assert chain.risk_score == 42.0
    assert chain.risk_level == "confirm"
    assert chain.confirmation_record == {"approved": True, "level": "confirm"}
    assert chain.approval_chain == ["sales_manager"]
    assert chain.api_request == {"FormId": "SAL_Order"}
    assert chain.api_response["Result"]["ResponseStatus"]["IsSuccess"] is True


def test_build_provenance_defaults_are_graceful() -> None:
    trace = AgentTrace(
        user_input="hello",
        user_context=UserContext(user_id="u001", role="viewer"),
        user_id="u001",
    )

    chain = build_provenance(trace, _CALL)

    assert chain.llm_reasoning == ""
    assert chain.tool_call_params == {"bill_no": "SO1", "amount": 80000}
    assert chain.guard_check_results == []
    assert chain.risk_score == 0.0
    assert chain.risk_level == ""
    assert chain.confirmation_record is None
    assert chain.approval_chain == []
    assert chain.api_request == {}


def test_explicit_confirmation_overrides_trace() -> None:
    chain = build_provenance(
        _trace(),
        _CALL,
        confirmation_record={"approved": False, "message": "用户取消"},
    )

    assert chain.confirmation_record == {"approved": False, "message": "用户取消"}


@pytest.mark.parametrize(
    ("score", "expected"),
    [("42.5", 42.5), ("abc", 0.0), (True, 0.0), ([], 0.0)],
)
def test_risk_score_is_normalized(score: object, expected: float) -> None:
    trace = _trace(risk_assessment={"score": score, "level": "auto"})

    assert build_provenance(trace, _CALL).risk_score == expected


def test_non_list_approval_chain_becomes_empty() -> None:
    trace = _trace(permission_check={"required_approvers": "sales_manager"})

    assert build_provenance(trace, _CALL).approval_chain == []


def test_provenance_is_json_serializable() -> None:
    chain = build_provenance(_trace(), _CALL)

    payload = json.loads(json.dumps(chain.model_dump(mode="json")))
    assert payload["user_id"] == "u001"
    assert payload["risk_score"] == 42.0


# ── 报告渲染 ──


def test_render_provenance_report_contains_full_chain() -> None:
    chain = build_provenance(
        _trace(),
        _CALL,
        api_request={"FormId": "SAL_Order"},
        api_response={"IsSuccess": True},
    )

    text = render_provenance_report(_record(chain))

    assert "用户原话：把 SO1 金额改成 8 万" in text
    assert "修改 SO1 金额为 80000" in text
    assert "guard_permission: ✅ 通过" in text
    assert "风险评分：42.0（confirm）" in text
    assert "sales_manager" in text
    assert '"IsSuccess": true' in text
    assert "- FAmount: 100 → 80" in text
    assert GENESIS_HASH in text


def test_render_failed_record_has_no_diff() -> None:
    chain = build_provenance(_trace(), _CALL)

    text = render_provenance_report(
        _record(chain, status="failed", after_snapshot=None, field_diff=[])
    )

    assert "状态：failed" in text
    assert "（无变更）" in text


def test_render_report_without_guard_checks() -> None:
    trace = AgentTrace(
        user_input="x",
        user_context=UserContext(user_id="u001", role="viewer"),
        user_id="u001",
    )
    chain = build_provenance(trace, _CALL)

    text = render_provenance_report(_record(chain))

    assert "Guard 检查：" in text
    assert "（无记录）" in text


def test_render_empty_trace_report() -> None:
    assert render_trace_report([]) == "未找到对应的账本记录。"


# ── 按 trace / 单据查询 ──


def _ledger(tmp_path: Path) -> tuple[WriteLedger, ProvenanceChain]:
    store = SqliteWriteLedgerStore(tmp_path / "ledger.db")
    ledger = WriteLedger(store)
    chain = build_provenance(_trace(), _CALL)
    ledger.record_write(
        trace_id="t1",
        form_id="SAL_Order",
        business_key="SO1",
        action="edit",
        provenance=chain,
        before_snapshot={"FAmount": 100},
        after_snapshot={"FAmount": 80},
        timestamp=datetime(2026, 10, 18, 10, 0),
    )
    ledger.record_write(
        trace_id="t2",
        form_id="SAL_Order",
        business_key="SO1",
        action="submit",
        provenance=chain,
        before_snapshot={"FDocumentStatus": "A"},
        after_snapshot={"FDocumentStatus": "C"},
        timestamp=datetime(2026, 10, 18, 11, 0),
    )
    return ledger, chain


def test_reporter_by_trace_and_business_key(tmp_path: Path) -> None:
    ledger, _ = _ledger(tmp_path)
    reporter = ProvenanceReporter(ledger)

    by_trace = reporter.report_by_trace_id("t1")
    by_business = reporter.report_by_business_key("SO1")

    assert "（edit）" in by_trace
    assert "（submit）" not in by_trace
    assert by_business.count("【写操作溯源】") == 2
    assert reporter.report_by_trace_id("missing") == "未找到对应的账本记录。"
