"""M9-T02：SSE 端到端测试（事件顺序 / DLP / 心跳 / 断连）。"""

import asyncio
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from agent.events import AgentEvent
from api import streaming
from api.app import create_app
from api.deps import AppDeps
from config.settings import Settings
from infrastructure.exceptions import BudgetExceededError
from kingdee.client import KingdeeClient
from models.llm import LLMContext, PlanResult, ToolCallDecision, ToolSpec
from models.user import UserContext
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "dev-key-alice"
_ALICE = {"user_id": "alice", "role": "viewer", "data_scope": {}}
_HEADERS = {"X-API-Key": _ALICE_KEY}


class _SlowProvider(FakeProvider):
    """plan 阶段等待闸门，用于验证心跳与断连取消。"""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(response_queue=["ok"])
        self._gate = gate

    async def plan(self, context: LLMContext, tools: Sequence[ToolSpec]) -> PlanResult:
        await self._gate.wait()
        return await super().plan(context, tools)


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(base_url, tmp_path, {_ALICE_KEY: _ALICE})


async def _collect_events(response: httpx.Response) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(AgentEvent.model_validate_json(line[len("data: ") :]))
    return events


async def _stream_events(
    app: FastAPI,
    body: dict[str, object],
) -> list[AgentEvent]:
    async with (
        api_client(app) as http,
        http.stream("POST", "/v1/chat/stream", headers=_HEADERS, json=body) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return await _collect_events(response)


async def test_pure_chat_stream_order(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["你好呀"])
    app = create_app(_settings(tmp_path, sandbox_server), provider=provider, client=client)

    events = await _stream_events(app, {"message": "你好"})

    phases = [(event.phase, event.status) for event in events if event.event == "phase"]
    assert phases == [
        ("plan", "start"),
        ("plan", "success"),
        ("guard", "start"),
        ("guard", "success"),
        ("generate", "start"),
        ("generate", "success"),
    ]
    final = next(event for event in events if event.event == "final")
    assert final.content == "你好呀"
    assert final.trace_id
    assert final.session_id
    assert events[-1].event == "done"


async def test_tool_events_have_no_payload(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(
        plan_queue=[
            PlanResult(
                tool_calls=[
                    ToolCallDecision(
                        tool_name="erp_query_sales_order",
                        arguments={"bill_no": "SO000001"},
                    )
                ]
            )
        ],
        response_queue=["查询完成"],
    )
    app = create_app(_settings(tmp_path, sandbox_server), provider=provider, client=client)

    events = await _stream_events(app, {"message": "查询销售订单 SO000001"})

    tool_events = [event for event in events if event.event == "tool"]
    assert [event.status for event in tool_events] == ["start", "success"]
    for event in tool_events:
        payload = event.model_dump(exclude_defaults=True)
        assert set(payload) <= {"event", "tool_name", "status", "latency_ms"}


async def test_final_matches_sync_response(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    sync_app = create_app(
        settings,
        provider=FakeProvider(response_queue=["一致答复"]),
        client=client,
    )
    stream_app = create_app(
        settings,
        provider=FakeProvider(response_queue=["一致答复"]),
        client=client,
    )

    async with api_client(sync_app) as http:
        sync_response = await http.post("/v1/chat", headers=_HEADERS, json={"message": "你好"})
    events = await _stream_events(stream_app, {"message": "你好"})
    final = next(event for event in events if event.event == "final")

    assert final.content == sync_response.json()["content"]


async def test_dlp_blocks_stream_and_hides_pii(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["客户电话 13800138000，请联系"])
    app = create_app(_settings(tmp_path, sandbox_server), provider=provider, client=client)

    events = await _stream_events(app, {"message": "查客户"})
    final = next(event for event in events if event.event == "final")
    raw_stream = "\n".join(event.model_dump_json() for event in events)

    assert final.blocked is True
    assert "13800138000" not in raw_stream


async def test_stream_requires_auth(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        response = await http.post("/v1/chat/stream", json={"message": "你好"})

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "unauthorized"


async def test_budget_error_becomes_event(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(plan_queue=[BudgetExceededError("预算耗尽")])
    app = create_app(_settings(tmp_path, sandbox_server), provider=provider, client=client)

    events = await _stream_events(app, {"message": "你好"})

    assert any(event.event == "error" and event.code == "budget_exhausted" for event in events)
    assert all(event.event != "final" for event in events)
    assert events[-1].event == "done"


async def test_heartbeat_while_waiting(
    tmp_path: Path,
    sandbox_server: str,
    client: KingdeeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streaming, "_HEARTBEAT_SECONDS", 0.01)
    gate = asyncio.Event()
    provider = _SlowProvider(gate)
    app = create_app(_settings(tmp_path, sandbox_server), provider=provider, client=client)
    user = UserContext(user_id="alice", role="viewer")

    seen_ping = False
    async with app.router.lifespan_context(app):
        deps: AppDeps = app.state.deps
        stream = streaming._stream_chat(deps, user, deps.store_for("alice"), "hb-1", "你好")
        async with asyncio.timeout(5):
            async for chunk in stream:
                if chunk == ": ping\n\n":
                    seen_ping = True
                    gate.set()
                if '"event":"done"' in chunk:
                    break

    assert seen_ping is True


async def test_disconnect_does_not_save_session(
    tmp_path: Path,
    sandbox_server: str,
    client: KingdeeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streaming, "_HEARTBEAT_SECONDS", 0.01)
    settings = _settings(tmp_path, sandbox_server)
    gate = asyncio.Event()  # 永不打开：生产者卡在 plan
    provider = _SlowProvider(gate)
    app = create_app(settings, provider=provider, client=client)
    user = UserContext(user_id="alice", role="viewer")
    session_id = "disc-1"

    async with app.router.lifespan_context(app):
        deps: AppDeps = app.state.deps
        stream = streaming._stream_chat(deps, user, deps.store_for("alice"), session_id, "你好")
        async with asyncio.timeout(5):
            first = await anext(stream)
        assert first.startswith("event: ")
        await stream.aclose()  # 模拟客户端断连

    await asyncio.sleep(0.05)
    assert not (settings.session_store_dir / "users" / "alice" / f"{session_id}.json").exists()
