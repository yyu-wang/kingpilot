"""M9-T06：看板 API 权限、范围与响应契约测试。"""

from pathlib import Path

from api.app import create_app
from config.settings import Settings
from kingdee.client import KingdeeClient
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ADMIN_KEY = "key-admin"
_MANAGER_KEY = "key-manager"
_ADMIN = {"user_id": "admin", "role": "admin", "data_scope": {}}
_MANAGER = {"user_id": "mgr-a", "role": "manager", "data_scope": {}}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {_ADMIN_KEY: _ADMIN, _MANAGER_KEY: _MANAGER},
    )


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


async def test_admin_can_read_dashboard(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        response = await http.get(
            "/v1/dashboard/overview",
            headers=_headers(_ADMIN_KEY),
            params={"from": "2026-10-18", "to": "2026-10-20"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["from_date"] == "2026-10-18"
    assert body["to_date"] == "2026-10-20"
    assert body["timezone"] == "Asia/Shanghai"
    assert len(body["daily"]) == 3
    assert body["totals"]["requests"] == 0
    assert "user_id" not in response.text
    assert "trace_id" not in response.text
    assert "user_input" not in response.text


async def test_manager_is_forbidden(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        response = await http.get("/v1/dashboard/overview", headers=_headers(_MANAGER_KEY))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_invalid_range(tmp_path: Path, sandbox_server: str, client: KingdeeClient) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        reversed_range = await http.get(
            "/v1/dashboard/overview",
            headers=_headers(_ADMIN_KEY),
            params={"from": "2026-10-20", "to": "2026-10-18"},
        )
        too_long = await http.get(
            "/v1/dashboard/overview",
            headers=_headers(_ADMIN_KEY),
            params={"from": "2026-01-01", "to": "2026-06-01"},
        )

    assert reversed_range.status_code == 422
    assert reversed_range.json()["error"]["code"] == "invalid_range"
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "invalid_range"


async def test_default_range_is_seven_days(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = create_app(_settings(tmp_path, sandbox_server), provider=FakeProvider(), client=client)

    async with api_client(app) as http:
        response = await http.get("/v1/dashboard/overview", headers=_headers(_ADMIN_KEY))

    assert response.status_code == 200
    assert len(response.json()["daily"]) == 7
