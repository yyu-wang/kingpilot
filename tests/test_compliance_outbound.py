"""M7-T05：全链路出域合规验收。

目标：一次完整会话结束后，所有出域工件（响应/Trace/审计/日志/记忆/
发给 LLM 的上下文）均不含原始敏感信息，DLP 零命中。

覆盖正常查询、LLM 幻觉、LLM 降级、多轮记忆四类路径；
首条用例反向自检扫描器，防止「零命中」因扫描失效而变成空测试。
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from agent.core import AgentCore
from agent.memory import MemoryStore
from agent.tools import build_default_registry
from config.settings import Settings
from infrastructure.exceptions import LLMUnavailableError
from infrastructure.logger import _inject_trace_id
from kingdee.client import KingdeeClient
from models.kingdee import BillQueryParams, BillQueryResult, JsonScalar
from models.llm import (
    PlanResult,
    ReflectionResult,
    ToolCallDecision,
)
from models.user import UserContext
from observability.audit import AuditLogger, JsonlAuditSink
from observability.tracing import TraceStore
from safety.dlp import DlpScanner
from safety.engine import SafetyLayer
from tests.fakes import FakeProvider

_TOOL = "kingdee_query_sales_order"
_RAW_PHONE = "13812345678"
_RAW_ID_CARD = "110101199001011234"
_RAW_BANK_CARD = "6222021234567890123"
_RAW_EMAIL = "alice@example.com"
_RAW_CONTACT = "张三"
_RAW_ADDRESS = "深圳市南山区科技园"

_PII_ROW: dict[str, JsonScalar] = {
    "FBillNo": "SO000008",
    "FPhone": _RAW_PHONE,
    "FIDCard": _RAW_ID_CARD,
    "FBankAccount": _RAW_BANK_CARD,
    "FEmail": _RAW_EMAIL,
    "FContact": _RAW_CONTACT,
    "FAddress": _RAW_ADDRESS,
    "FAmount": 1000,
}


class _PiiClient:
    """返回含全部分级敏感字段的查询结果。"""

    async def execute_bill_query(self, params: BillQueryParams) -> BillQueryResult:
        return BillQueryResult(
            form_id=params.form_id,
            field_keys=params.field_keys.split(","),
            rows=[dict(_PII_ROW)],
        )


class _FakeIO:
    async def ask(self, prompt: str) -> bool:
        return True


@dataclass
class _Harness:
    core: AgentCore
    provider: FakeProvider
    memory: MemoryStore
    audit_path: Path
    trace_store: TraceStore


def _settings() -> Settings:
    return Settings(_env_file=None, agent_max_steps=2)


def _manager() -> UserContext:
    return UserContext(user_id="u001", role="manager")


def _plan() -> PlanResult:
    return PlanResult(
        tool_calls=[ToolCallDecision(tool_name=_TOOL, arguments={"bill_no": "SO000008"})]
    )


def _stop() -> ReflectionResult:
    return ReflectionResult(continue_execution=False)


def _build_harness(
    tmp_path: Path,
    provider: FakeProvider,
    *,
    memory: MemoryStore | None = None,
) -> _Harness:
    registry = build_default_registry()
    client = _PiiClient()
    client_for_core = cast(KingdeeClient, client)
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(JsonlAuditSink(audit_path))
    trace_store = TraceStore(tmp_path / "traces")
    resolved_memory = memory or MemoryStore(session_id="compliance")
    safety = SafetyLayer(
        registry,
        client_for_core,
        _FakeIO(),
        clock=lambda: datetime(2026, 10, 19, 10, 0),
    )
    core = AgentCore(
        provider,
        registry,
        client_for_core,
        _settings(),
        memory=resolved_memory,
        safety=safety,
        trace_store=trace_store,
        audit=audit,
    )
    return _Harness(core, provider, resolved_memory, audit_path, trace_store)


@contextmanager
def _captured_logs() -> Iterator[list[str]]:
    # 显式装配 patcher：日志出口的 PII 掩码是生产行为（M7-T01/T05）
    logger.configure(patcher=_inject_trace_id)
    lines: list[str] = []
    handler_id = logger.add(
        lambda message: lines.append(message), format="{message}", level="DEBUG"
    )
    try:
        yield lines
    finally:
        logger.remove(handler_id)


def _assert_clean(text: str, where: str) -> None:
    result = DlpScanner().scan(text)
    assert not result.blocked, f"{where} 仍含敏感信息: {result.categories}"


def _memory_text(memory: MemoryStore) -> str:
    messages = [asdict(message) for message in memory.raw_messages()]
    entities = [asdict(entity) for entity in memory.entities()]
    return json.dumps({"messages": messages, "entities": entities}, ensure_ascii=False, default=str)


def _llm_context_text(provider: FakeProvider) -> str:
    parts: list[str] = []
    for context in (provider.last_plan_context, provider.last_generate_context):
        if context is not None:
            parts.append(context.model_dump_json())
    if provider.last_reflect_summary is not None:
        parts.append(provider.last_reflect_summary.model_dump_json())
    if provider.last_reflect_memory is not None:
        parts.append(provider.last_reflect_memory.model_dump_json())
    return "\n".join(parts)


def _artifacts(harness: _Harness, response: Any, logs: list[str]) -> dict[str, str]:
    trace_file = harness.trace_store.path_for(response.trace_id)
    audit_text = (
        harness.audit_path.read_text(encoding="utf-8") if harness.audit_path.exists() else ""
    )
    return {
        "响应内容": response.content,
        "Trace 内存": response.trace.model_dump_json(),
        "Trace 落盘": trace_file.read_text(encoding="utf-8"),
        "审计文件": audit_text,
        "日志输出": "\n".join(logs),
        "记忆写回": _memory_text(harness.memory),
        "LLM 上下文": _llm_context_text(harness.provider),
    }


# ── 反向自检：扫描器必须能命中参考数据 ──


def test_reference_pii_is_detected() -> None:
    result = DlpScanner().scan(json.dumps(_PII_ROW, ensure_ascii=False))
    assert result.blocked is True
    assert {"phone", "id_card", "bank_card", "email"} <= set(result.categories)


# ── 正常查询：全部出域工件零命中 ──


async def test_normal_query_leaves_all_artifacts_clean(tmp_path: Path) -> None:
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=["联系人 张*，电话 138****5678，银行卡 [REDACTED]，邮箱 a***@example.com"],
    )
    harness = _build_harness(tmp_path, provider)

    with _captured_logs() as logs:
        response = await harness.core.run("查订单 SO000008", _manager())

    assert response.blocked is False
    assert response.trace is not None
    row = response.trace.tool_executions[0].result["rows"][0]
    assert row["FPhone"] == "138****5678"
    assert row["FIDCard"] == "[REDACTED]"
    assert row["FContact"] == "张*"

    for where, text in _artifacts(harness, response, logs).items():
        _assert_clean(text, where)


async def test_tool_result_is_masked_before_reaching_llm(tmp_path: Path) -> None:
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=["已查询完成"],
    )
    harness = _build_harness(tmp_path, provider)

    await harness.core.run("查订单 SO000008", _manager())

    context = _llm_context_text(provider)
    assert "138****5678" in context
    assert _RAW_PHONE not in context
    assert _RAW_ID_CARD not in context
    assert _RAW_BANK_CARD not in context


# ── LLM 幻觉：命中阻断且不留原文 ──


async def test_hallucinated_pii_is_blocked_everywhere(tmp_path: Path) -> None:
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=[f"客户电话 {_RAW_PHONE}，身份证 {_RAW_ID_CARD}，卡号 {_RAW_BANK_CARD}"],
    )
    harness = _build_harness(tmp_path, provider)

    with _captured_logs() as logs:
        response = await harness.core.run("查订单 SO000008", _manager())

    assert response.blocked is True
    assert "已按数据安全策略阻断发送" in response.content
    # 记忆中的 assistant 原文同步被替换（否则会再次进入后续 LLM 上下文）
    assert harness.memory.raw_messages()[-1].content == response.content

    for where, text in _artifacts(harness, response, logs).items():
        _assert_clean(text, where)

    events = [
        json.loads(line) for line in harness.audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event_type"] for event in events] == ["dlp.blocked"]


# ── LLM 降级：确定性渲染同样零命中 ──


async def test_fallback_path_leaves_no_pii(tmp_path: Path) -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("模拟宕机")])
    harness = _build_harness(tmp_path, provider)

    with _captured_logs() as logs:
        response = await harness.core.run("查订单 SO000008", _manager())

    assert response.trace is not None
    assert response.trace.used_fallback is True
    assert _RAW_PHONE not in response.content
    assert _RAW_ID_CARD not in response.content
    assert _RAW_BANK_CARD not in response.content

    for where, text in _artifacts(harness, response, logs).items():
        _assert_clean(text, where)


# ── 多轮会话：记忆与多份 Trace 均不残留 ──


async def test_multi_turn_session_stays_clean(tmp_path: Path) -> None:
    provider = FakeProvider(
        plan_queue=[_plan(), _plan()],
        reflection_queue=[_stop(), _stop()],
        response_queue=["第一轮 138****5678", "第二轮 张*"],
    )
    memory = MemoryStore(session_id="compliance-multi")
    harness = _build_harness(tmp_path, provider, memory=memory)

    with _captured_logs() as logs:
        first = await harness.core.run("查订单 SO000008", _manager())
        second = await harness.core.run("再查订单 SO000009", _manager())

    trace_files = sorted((tmp_path / "traces").glob("*.json"))
    assert len(trace_files) == 2
    _assert_clean(_memory_text(memory), "多轮记忆")
    for path in trace_files:
        _assert_clean(path.read_text(encoding="utf-8"), f"Trace {path.name}")
    _assert_clean(first.content, "第一轮响应")
    _assert_clean(second.content, "第二轮响应")
    _assert_clean("\n".join(logs), "多轮日志")
