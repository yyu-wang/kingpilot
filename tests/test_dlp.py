"""M7-T02：DLP 出域扫描单测。

覆盖四类敏感信息识别、身份证/银行卡重叠去重，以及主循环出口的
整条阻断、审计事件、Trace 落盘净化与干净答复放行。
"""

import json
from pathlib import Path
from typing import cast

from agent.core import AgentCore
from agent.tools import build_default_registry
from config.settings import Settings
from kingdee.client import KingdeeClient
from observability.audit import AuditLogger, JsonlAuditSink
from observability.tracing import TraceStore
from safety.dlp import DlpFinding, DlpScanner
from tests.fakes import FakeProvider

_PHONE = "13812345678"
_ID_CARD = "110101199001011234"
_BANK_CARD = "6222021234567890123"
_EMAIL = "alice@example.com"
_BLOCK_HINT = "已按数据安全策略阻断发送"


def _settings() -> Settings:
    # _env_file=None：防本机 .env 污染（T03 教训）
    return Settings(_env_file=None, agent_max_steps=2)


def _core(
    response: str,
    *,
    audit: AuditLogger | None = None,
    trace_store: TraceStore | None = None,
) -> AgentCore:
    provider = FakeProvider(response_queue=[response])
    return AgentCore(
        provider,
        build_default_registry(),
        cast(KingdeeClient, None),
        _settings(),
        audit=audit,
        trace_store=trace_store,
    )


# ── 扫描器：四类识别 ──


def test_normal_text_is_not_blocked() -> None:
    result = DlpScanner().scan("订单 SO000008 金额 1000 元，交货日期 2026-10-20")
    assert result.blocked is False
    assert result.findings == ()


def test_phone_is_detected() -> None:
    result = DlpScanner().scan(f"联系电话：{_PHONE}")
    assert result.blocked is True
    assert result.findings == (DlpFinding(category="phone", count=1),)


def test_id_card_is_detected() -> None:
    result = DlpScanner().scan(f"身份证：{_ID_CARD}")
    assert result.categories == ("id_card",)
    assert result.total == 1


def test_bank_card_is_detected() -> None:
    result = DlpScanner().scan(f"卡号 {_BANK_CARD}")
    assert result.categories == ("bank_card",)
    assert result.total == 1


def test_email_is_detected() -> None:
    result = DlpScanner().scan(f"邮箱：{_EMAIL}")
    assert result.categories == ("email",)


def test_id_card_is_not_double_counted_as_bank_card() -> None:
    """18 位身份证同时满足银行卡正则，跨度去重后只能报 id_card。"""
    result = DlpScanner().scan(_ID_CARD)
    assert result.categories == ("id_card",)
    assert result.total == 1


def test_multiple_categories_and_empty_text() -> None:
    assert DlpScanner().scan("").blocked is False

    result = DlpScanner().scan(f"手机 {_PHONE}，邮箱 {_EMAIL}")
    assert result.categories == ("phone", "email")
    assert result.total == 2


# ── 主循环出口：阻断 / 审计 / 落盘 ──


async def test_dlp_hit_blocks_whole_response() -> None:
    core = _core(f"客户手机号是 {_PHONE}")

    response = await core.run("查客户")

    assert response.blocked is True
    assert _BLOCK_HINT in response.content
    assert _PHONE not in response.content
    assert response.reason == response.content
    assert response.hallucination_warning is False
    # Trace 同步净化：原文不随 trace 留存在内存对象里
    assert response.trace is not None
    assert response.trace.final_response == response.content


async def test_dlp_hit_emits_audit_without_raw_value(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(JsonlAuditSink(audit_path))
    core = _core(f"身份证 {_ID_CARD}", audit=audit)

    response = await core.run("查客户")

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "dlp.blocked"
    assert event["status"] == "blocked"
    assert event["trace_id"] == str(response.trace_id)
    assert event["detail"]["categories"] == ["id_card"]
    assert event["detail"]["total"] == 1
    assert _ID_CARD not in audit_path.read_text(encoding="utf-8")


async def test_clean_response_passes_and_writes_no_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(JsonlAuditSink(audit_path))
    core = _core("订单 SO000008 已发货", audit=audit)

    response = await core.run("查订单")

    assert response.blocked is False
    assert response.content == "订单 SO000008 已发货"
    assert audit_path.exists() is False


async def test_trace_file_contains_no_pii(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")
    core = _core(f"银行卡 {_BANK_CARD}", trace_store=store)

    response = await core.run("查客户")

    trace_file = store.path_for(response.trace_id)
    saved = trace_file.read_text(encoding="utf-8")
    assert _BANK_CARD not in saved
    assert _BLOCK_HINT in saved


async def test_multiple_pii_reports_all_categories(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(JsonlAuditSink(audit_path))
    core = _core(f"手机 {_PHONE}，邮箱 {_EMAIL}", audit=audit)

    await core.run("查客户联系人")

    event = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert event["detail"]["categories"] == ["phone", "email"]
    assert event["detail"]["total"] == 2
