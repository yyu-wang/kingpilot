"""M9-T01：API Key 认证与配置校验。"""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from api.app import create_app
from api.auth import ApiKeyDirectory
from config.settings import Settings
from infrastructure.exceptions import ApiKeyConfigError
from kingdee.client import KingdeeClient
from observability.tracing import TraceStore
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "dev-key-alice"
_ALICE = {"user_id": "alice", "role": "viewer", "data_scope": {}}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(base_url, tmp_path, {_ALICE_KEY: _ALICE})


def _hash_of(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _directory(identity: dict[str, object]) -> ApiKeyDirectory:
    payload = {_hash_of("some-key"): {"tenant_id": "default", **identity}}
    return ApiKeyDirectory.from_settings(
        Settings(_env_file=None, api_key_hashes=SecretStr(json.dumps(payload)))
    )


async def test_health_is_public(tmp_path: Path, sandbox_server: str, client: KingdeeClient) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        resp = await http.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_missing_key_returns_401(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        resp = await http.get("/v1/sessions")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.headers["www-authenticate"] == "X-API-Key"


async def test_wrong_key_returns_401(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        resp = await http.get("/v1/sessions", headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_empty_key_config_rejects_all(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = api_settings(sandbox_server, tmp_path, {})
    app = create_app(settings, provider=FakeProvider(), client=client)
    async with api_client(app) as http:
        resp = await http.get("/v1/sessions", headers={"X-API-Key": _ALICE_KEY})
    assert resp.status_code == 401


async def test_valid_key_chat_and_trace(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    provider = FakeProvider(response_queue=["你好，我是助手"])
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(settings, provider=provider, client=client)

    async with api_client(app) as http:
        resp = await http.post(
            "/v1/chat",
            headers={"X-API-Key": _ALICE_KEY},
            json={"message": "你好"},
        )

    assert resp.status_code == 200
    body = resp.json()
    trace = TraceStore(settings.trace_store_dir).load(body["trace_id"])
    assert trace.user_id == "alice"
    assert trace.session_id == body["session_id"]
    assert trace.final_response == "你好，我是助手"


def test_invalid_json_config_fails_closed() -> None:
    settings = Settings(_env_file=None, api_key_hashes=SecretStr("not-json"))
    with pytest.raises(ApiKeyConfigError):
        ApiKeyDirectory.from_settings(settings)


def test_invalid_user_id_fails_closed() -> None:
    with pytest.raises(ApiKeyConfigError):
        _directory({"user_id": "../evil", "role": "viewer"})


def test_invalid_role_fails_closed() -> None:
    with pytest.raises(ApiKeyConfigError):
        _directory({"user_id": "alice", "role": "root"})


def test_missing_tenant_fails_closed() -> None:
    """M9-T11：API Key 身份缺 tenant_id → 启动失败（默认拒绝）。"""
    payload = {_hash_of("some-key"): {"user_id": "alice", "role": "viewer"}}
    settings = Settings(_env_file=None, api_key_hashes=SecretStr(json.dumps(payload)))
    with pytest.raises(ApiKeyConfigError):
        ApiKeyDirectory.from_settings(settings)


def test_invalid_tenant_fails_closed() -> None:
    with pytest.raises(ApiKeyConfigError):
        _directory({"user_id": "alice", "tenant_id": "../evil", "role": "viewer"})


def test_invalid_hash_format_fails_closed() -> None:
    payload = {"not-a-hash": {"user_id": "alice", "role": "viewer"}}
    settings = Settings(_env_file=None, api_key_hashes=SecretStr(json.dumps(payload)))
    with pytest.raises(ApiKeyConfigError):
        ApiKeyDirectory.from_settings(settings)
