"""M9-T01：对话、多轮记忆、DLP 与 LLM 降级。"""

from pathlib import Path

from agent.session_store import SessionStore
from api.app import create_app
from config.settings import Settings
from infrastructure.exceptions import LLMUnavailableError
from kingdee.client import KingdeeClient
from models.llm import PlanResult
from observability.tracing import TraceStore
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "dev-key-alice"
_ALICE = {"user_id": "alice", "role": "viewer", "data_scope": {}}
_HEADERS = {"X-API-Key": _ALICE_KEY}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(base_url, tmp_path, {_ALICE_KEY: _ALICE})


async def test_single_turn_returns_content(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["查询完成"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        resp = await http.post("/v1/chat", headers=_HEADERS, json={"message": "你好"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "查询完成"
    assert body["session_id"]
    assert body["trace_id"]


async def test_multi_turn_keeps_memory(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(
        plan_queue=[PlanResult(tool_calls=[]), PlanResult(tool_calls=[])],
        response_queue=["第一轮", "第二轮"],
    )
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        first = await http.post("/v1/chat", headers=_HEADERS, json={"message": "第一句"})
        session_id = first.json()["session_id"]
        second = await http.post(
            "/v1/chat",
            headers=_HEADERS,
            json={"message": "第二句", "session_id": session_id},
        )

    assert second.status_code == 200
    assert second.json()["session_id"] == session_id
    memory = SessionStore(settings.session_store_dir / "users" / "alice").load(session_id)
    assert memory.raw_rounds == 2


async def test_generated_session_is_persisted(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["ok"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        resp = await http.post("/v1/chat", headers=_HEADERS, json={"message": "你好"})

    session_id = resp.json()["session_id"]
    path = settings.session_store_dir / "users" / "alice" / f"{session_id}.json"
    assert path.exists()


async def test_dlp_blocks_and_does_not_leak(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["客户电话 13800138000，请联系"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        resp = await http.post("/v1/chat", headers=_HEADERS, json={"message": "查客户"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    assert "13800138000" not in resp.text

    trace = TraceStore(settings.trace_store_dir).load(body["trace_id"])
    assert "13800138000" not in trace.final_response

    audit_text = settings.audit_log_path.read_text(encoding="utf-8")
    assert "dlp.blocked" in audit_text
    assert "13800138000" not in audit_text


async def test_llm_unavailable_falls_back(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(plan_queue=[LLMUnavailableError("down")])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        resp = await http.post("/v1/chat", headers=_HEADERS, json={"message": "你好"})

    assert resp.status_code == 200
    body = resp.json()
    assert "LLM 暂不可用" in body["content"]
    trace = TraceStore(settings.trace_store_dir).load(body["trace_id"])
    assert trace.used_fallback is True


async def test_blank_message_returns_422(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        resp = await http.post("/v1/chat", headers=_HEADERS, json={"message": "   "})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "empty_message"
