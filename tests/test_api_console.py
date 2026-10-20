"""M9-T03：控制台静态资源、安全头、会话历史与 Trace 接口。"""

import json
from pathlib import Path

import httpx
import pytest

from api.app import create_app
from config.settings import Settings
from kingdee.client import KingdeeClient
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "dev-key-alice"
_BOB_KEY = "dev-key-bob"
_ALICE = {"user_id": "alice", "role": "viewer", "data_scope": {}}
_BOB = {"user_id": "bob", "role": "viewer", "data_scope": {}}
_STATIC_DIR = Path(__file__).resolve().parent.parent / "api" / "static"


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {_ALICE_KEY: _ALICE, _BOB_KEY: _BOB},
    )


async def _create_chat(
    http: httpx.AsyncClient,
    *,
    key: str = _ALICE_KEY,
    message: str = "你好",
) -> dict[str, object]:
    response = await http.post(
        "/v1/chat",
        headers={"X-API-Key": key},
        json={"message": message},
    )
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


async def test_root_redirects_to_console(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        response = await http.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/console/"


async def test_console_assets_are_served(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        index = await http.get("/console/")
        script = await http.get("/console/app.js")
        styles = await http.get("/console/styles.css")

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "<title>KingPilot" in index.text
    assert script.status_code == 200
    assert styles.status_code == 200


def test_static_assets_have_no_external_dependencies() -> None:
    for name in ("index.html", "app.js", "styles.css"):
        text = (_STATIC_DIR / name).read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text
    script = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "eval(" not in script


async def test_security_headers(tmp_path: Path, sandbox_server: str, client: KingdeeClient) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        response = await http.get("/v1/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_session_messages_are_redacted_and_owner_only(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["收到"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        created = await _create_chat(http, message="我的手机号 13800138000")
        session_id = created["session_id"]
        owner = await http.get(
            f"/v1/sessions/{session_id}/messages",
            headers={"X-API-Key": _ALICE_KEY},
        )
        stranger = await http.get(
            f"/v1/sessions/{session_id}/messages",
            headers={"X-API-Key": _BOB_KEY},
        )

    assert owner.status_code == 200
    messages = owner.json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    # 历史 assistant 消息带 Trace 链接（控制台「查看 Trace」按钮）
    assistant = next(message for message in messages if message["role"] == "assistant")
    assert assistant["trace_id"] == created["trace_id"]
    assert "13800138000" not in owner.text
    assert "138****8000" in owner.text
    assert stranger.status_code == 404


async def test_legacy_session_messages_backfill_trace_id(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    """旧会话文件没有 trace_id 时，按回答文本从 Trace 反查补齐。"""
    provider = FakeProvider(response_queue=["收到"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        created = await _create_chat(http, message="你好")
        session_id = str(created["session_id"])
        path = Path(settings.session_store_dir) / "users" / "alice" / f"{session_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload["raw_messages"]:
            item.pop("trace_id", None)  # 模拟 M9-T13 之前落盘的会话
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        response = await http.get(
            f"/v1/sessions/{session_id}/messages",
            headers={"X-API-Key": _ALICE_KEY},
        )

    assert response.status_code == 200
    assistant = next(
        message for message in response.json()["messages"] if message["role"] == "assistant"
    )
    assert assistant["trace_id"] == created["trace_id"]


async def test_session_messages_missing_returns_404(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        response = await http.get(
            "/v1/sessions/not-exist/messages",
            headers={"X-API-Key": _ALICE_KEY},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


async def test_trace_list_is_owner_filtered(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["查询完成"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        created = await _create_chat(http, message="查询销售订单 SO000001")
        session_id = created["session_id"]
        owner = await http.get(
            f"/v1/traces?session_id={session_id}",
            headers={"X-API-Key": _ALICE_KEY},
        )
        stranger = await http.get(
            f"/v1/traces?session_id={session_id}",
            headers={"X-API-Key": _BOB_KEY},
        )

    assert owner.status_code == 200
    traces = owner.json()["traces"]
    assert len(traces) == 1
    assert traces[0]["trace_id"] == created["trace_id"]
    assert traces[0]["session_id"] == session_id
    assert stranger.status_code == 200
    assert stranger.json()["traces"] == []


async def test_trace_detail_is_sanitized_and_owner_only(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["收到"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        created = await _create_chat(http, message="我的手机号 13800138000")
        trace_id = created["trace_id"]
        owner = await http.get(
            f"/v1/traces/{trace_id}",
            headers={"X-API-Key": _ALICE_KEY},
        )
        stranger = await http.get(
            f"/v1/traces/{trace_id}",
            headers={"X-API-Key": _BOB_KEY},
        )

    assert owner.status_code == 200
    body = owner.json()
    assert "Trace View" in body["report_text"]
    assert body["user_input"] == "我的手机号 138****8000"
    assert "13800138000" not in owner.text
    assert stranger.status_code == 404
    assert stranger.json()["error"]["code"] == "trace_not_found"


async def test_trace_detail_missing_returns_404(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        response = await http.get(
            "/v1/traces/not-a-trace",
            headers={"X-API-Key": _ALICE_KEY},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "trace_not_found"


@pytest.mark.parametrize(
    "path",
    ["/console/", "/console/app.js", "/console/styles.css", "/console/favicon.svg"],
)
async def test_static_assets_are_reachable(
    path: str,
    tmp_path: Path,
    sandbox_server: str,
    client: KingdeeClient,
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        response = await http.get(path)
    assert response.status_code == 200
