"""M6-T03：Trace 落盘与报告单测。

覆盖 save/load 往返、路径安全、损坏/版本/ID 校验、列表、决策树报告、
长结果截断与主循环落盘。
"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from agent.core import AgentCore
from agent.tools import ToolRegistry
from config.settings import Settings
from infrastructure.exceptions import LLMUnavailableError, TraceStoreError
from models.agent import AgentTrace
from models.llm import PlanResult
from models.tool import ToolExecution
from models.user import UserContext
from observability.tracing import TraceStore, render_trace_report
from tests.fakes import FakeProvider


def _trace() -> AgentTrace:
    return AgentTrace(
        user_input="查订单 SO1",
        user_context=UserContext(user_id="u001", role="operator"),
        user_id="u001",
        session_id="s1",
        llm_planning={
            "reasoning": "查询订单",
            "tool_calls": [{"tool_name": "erp_query_sales_order", "arguments": {"bill_no": "SO1"}}],
        },
        guard_checks=[
            {"guard": "guard_schema", "passed": True, "reason": ""},
            {"guard": "guard_sql", "passed": False, "reason": "字段越权"},
        ],
        risk_assessment={
            "score": 42.0,
            "level": "confirm",
            "required_approvers": ["manager"],
        },
        permission_check={
            "allowed": True,
            "domain": "sales",
            "row_filter": "FSaleOrgId.FNumber = 'ORG1'",
        },
        tool_executions=[
            ToolExecution(
                tool_name="erp_query_sales_order",
                parameters={"bill_no": "SO1"},
                result={"field_keys": ["FBillNo"], "rows": [{"FBillNo": "SO1"}]},
                latency_ms=120,
            )
        ],
        reflections=[{"continue_execution": True, "next_input": "再查一次"}],
        final_response="查到 1 条订单",
        total_latency_ms=850,
        llm_token_usage={
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
        kingdee_api_calls=1,
    )


def _store(tmp_path: Path) -> TraceStore:
    return TraceStore(tmp_path / "traces")


# ── 存储 ──


def test_save_load_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trace = _trace()

    path = store.save(trace)
    loaded = store.load(trace.trace_id)

    assert path.exists()
    assert loaded.trace_id == trace.trace_id
    assert loaded.user_input == "查订单 SO1"
    assert loaded.llm_planning["reasoning"] == "查询订单"
    assert len(loaded.guard_checks) == 2
    assert loaded.tool_executions[0].tool_name == "erp_query_sales_order"
    assert loaded.reflections[0]["next_input"] == "再查一次"
    assert loaded.total_latency_ms == 850


def test_illegal_trace_id_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(TraceStoreError, match="非法 trace_id"):
        store.path_for("../evil")


def test_root_property(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.root == tmp_path / "traces"


def test_save_oserror_raises(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    store = TraceStore(blocked)

    with pytest.raises(TraceStoreError, match="写入失败"):
        store.save(_trace())


def test_load_missing_trace_raises(tmp_path: Path) -> None:
    with pytest.raises(TraceStoreError, match="不存在"):
        _store(tmp_path).load(uuid4())


def test_missing_trace_payload_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trace_id = uuid4()
    path = store.path_for(trace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    with pytest.raises(TraceStoreError, match="内容缺失"):
        store.load(trace_id)


def test_invalid_trace_payload_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trace_id = uuid4()
    path = store.path_for(trace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "trace": {}}), encoding="utf-8")

    with pytest.raises(TraceStoreError, match="内容不合法"):
        store.load(trace_id)


def test_list_traces_on_missing_root(tmp_path: Path) -> None:
    assert TraceStore(tmp_path / "missing").list_traces() == []


def test_corrupt_file_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.path_for(uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(TraceStoreError, match="损坏"):
        store.load(path.stem)


def test_schema_mismatch_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trace_id = uuid4()
    path = store.path_for(trace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 2, "trace": {}}), encoding="utf-8")

    with pytest.raises(TraceStoreError, match="版本"):
        store.load(trace_id)


def test_trace_id_mismatch_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    trace = _trace()
    path = store.save(trace)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["trace"]["trace_id"] = str(uuid4())
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TraceStoreError, match="不一致"):
        store.load(trace.trace_id)


def test_list_traces_skips_corrupt_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_trace())
    store.save(_trace())
    bad = store.path_for(uuid4())
    bad.write_text("{bad", encoding="utf-8")

    infos = store.list_traces()

    assert len(infos) == 2
    assert all(info.user_input == "查订单 SO1" for info in infos)
    assert all(info.total_latency_ms == 850 for info in infos)


# ── 报告 ──


def test_report_contains_decision_tree() -> None:
    text = render_trace_report(_trace())

    assert "Trace View" in text
    assert "用户: u001 (operator)" in text
    assert "降级: 否" in text
    assert "思考: 查询订单" in text
    assert "guard_sql: ❌（字段越权）" in text
    assert "Risk: 42.0 → confirm" in text
    assert "FSaleOrgId.FNumber = 'ORG1'" in text
    assert "erp_query_sales_order (120ms) → 1 条" in text
    assert "continue=True" in text
    assert "Final: 查到 1 条订单" in text
    assert "850ms" in text


def test_report_shows_fallback_intent_and_reason() -> None:
    trace = _trace()
    trace.llm_planning = {
        "fallback": True,
        "intent": "sales_order",
        "reason": "timeout",
    }
    trace.used_fallback = True

    text = render_trace_report(trace)

    assert "降级: 是" in text
    assert "规则引擎兜底" in text
    assert "intent=sales_order" in text
    assert "reason=timeout" in text


def test_report_truncates_long_result() -> None:
    trace = _trace()
    trace.tool_executions = [
        ToolExecution(
            tool_name="erp_query_sales_order",
            parameters={},
            result={"rows": [{"FBillNo": "X" * 500}]},
        )
    ]

    text = render_trace_report(trace)

    assert "X" * 500 not in text
    assert "..." in text


def test_report_handles_empty_sections() -> None:
    trace = AgentTrace(
        user_input="你好",
        user_context=UserContext(user_id="u001", role="viewer"),
        user_id="u001",
    )

    text = render_trace_report(trace)

    assert "Guard Layer" in text
    assert text.count("（无）") >= 3
    assert "Saga: -" in text


def test_report_shows_saga_and_tool_error() -> None:
    trace = _trace()
    trace.saga_transaction = {
        "status": "rolled_back",
        "step_results": [{"step_id": "s1"}],
        "compensation_results": [{"step_id": "s1"}],
    }
    trace.tool_executions = [
        ToolExecution(tool_name="broken_tool", parameters={}, error="连接超时")
    ]

    text = render_trace_report(trace)

    assert "Saga: rolled_back（1 步 / 1 补偿）" in text
    assert "broken_tool → 失败：连接超时" in text


@pytest.mark.parametrize(
    ("score", "expected"),
    [("42", "42.0"), ("abc", "0.0"), (True, "0.0"), ([], "0.0")],
)
def test_report_normalizes_risk_score(score: object, expected: str) -> None:
    trace = _trace()
    trace.risk_assessment = {"score": score, "level": "auto"}

    text = render_trace_report(trace)

    assert f"Risk: {expected}" in text


# ── 主循环落盘 ──


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, trace_store_dir=tmp_path / "traces")


async def test_agent_core_saves_trace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = TraceStore(settings.trace_store_dir)
    provider = FakeProvider(plan_queue=[PlanResult(tool_calls=[])])
    core = AgentCore(
        provider,
        ToolRegistry(),
        client=None,  # type: ignore[arg-type]
        settings=settings,
        trace_store=store,
    )

    response = await core.run("你好")

    assert response.trace is not None
    loaded = store.load(response.trace.trace_id)
    assert loaded.user_input == "你好"


async def test_agent_core_saves_fallback_trace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = TraceStore(settings.trace_store_dir)
    provider = FakeProvider(plan_queue=[LLMUnavailableError("timeout")])
    core = AgentCore(
        provider,
        ToolRegistry(),
        client=None,  # type: ignore[arg-type]
        settings=settings,
        trace_store=store,
    )

    response = await core.run("你好")

    loaded = store.load(response.trace.trace_id) if response.trace else None
    assert loaded is not None
    assert loaded.used_fallback is True
