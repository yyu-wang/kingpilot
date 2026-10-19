"""M9-T01：会话列表与跨用户隔离。"""

from pathlib import Path

from api.app import create_app
from config.settings import Settings
from kingdee.client import KingdeeClient
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "dev-key-alice"
_BOB_KEY = "dev-key-bob"
_ALICE = {"user_id": "alice", "role": "viewer", "data_scope": {}}
_BOB = {"user_id": "bob", "role": "viewer", "data_scope": {}}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {_ALICE_KEY: _ALICE, _BOB_KEY: _BOB},
    )


async def test_sessions_are_isolated_between_users(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["alice 的答复"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        created = await http.post(
            "/v1/chat",
            headers={"X-API-Key": _ALICE_KEY},
            json={"message": "你好"},
        )
        session_id = created.json()["session_id"]

        alice_list = await http.get("/v1/sessions", headers={"X-API-Key": _ALICE_KEY})
        bob_list = await http.get("/v1/sessions", headers={"X-API-Key": _BOB_KEY})
        bob_get = await http.get(f"/v1/sessions/{session_id}", headers={"X-API-Key": _BOB_KEY})

    assert alice_list.status_code == 200
    assert [item["session_id"] for item in alice_list.json()["sessions"]] == [session_id]
    assert bob_list.status_code == 200
    assert bob_list.json()["sessions"] == []
    assert bob_get.status_code == 404
    assert bob_get.json()["error"]["code"] == "session_not_found"


async def test_session_detail_visible_to_owner(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["ok"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        created = await http.post(
            "/v1/chat",
            headers={"X-API-Key": _ALICE_KEY},
            json={"message": "你好"},
        )
        session_id = created.json()["session_id"]
        detail = await http.get(f"/v1/sessions/{session_id}", headers={"X-API-Key": _ALICE_KEY})

    assert detail.status_code == 200
    assert detail.json()["session_id"] == session_id
    assert detail.json()["rounds"] == 1


async def test_invalid_session_id_returns_400(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        resp = await http.get("/v1/sessions/bad$id", headers={"X-API-Key": _ALICE_KEY})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "session_invalid"


async def test_missing_session_returns_404(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        resp = await http.get("/v1/sessions/doesnotexist", headers={"X-API-Key": _ALICE_KEY})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"
