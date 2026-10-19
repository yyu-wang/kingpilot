"""M9-T01：统一错误信封与异常映射。"""

from pathlib import Path

from api.app import create_app
from config.settings import Settings
from infrastructure.exceptions import BudgetExceededError
from kingdee.client import KingdeeClient
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "dev-key-alice"
_ALICE = {"user_id": "alice", "role": "viewer", "data_scope": {}}
_HEADERS = {"X-API-Key": _ALICE_KEY}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(base_url, tmp_path, {_ALICE_KEY: _ALICE})


async def test_budget_exhausted_maps_to_503(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(plan_queue=[BudgetExceededError("预算耗尽")])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        resp = await http.post("/v1/chat", headers=_HEADERS, json={"message": "你好"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "budget_exhausted"


async def test_schema_validation_does_not_echo_input(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        resp = await http.post(
            "/v1/chat",
            headers=_HEADERS,
            json={"message": 12345},
        )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
    assert "12345" not in resp.text


async def test_unexpected_exception_is_sanitized(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(plan_queue=[RuntimeError("boom-secret")])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        resp = await http.post("/v1/chat", headers=_HEADERS, json={"message": "你好"})

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "boom-secret" not in resp.text


async def test_unknown_route_uses_error_envelope(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        resp = await http.get("/v1/unknown")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
