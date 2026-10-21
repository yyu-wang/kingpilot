"""M9-T04：审批中心 API 契约与可见性测试。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI

from api.app import create_app
from api.deps import AppDeps
from config.settings import Settings
from governance.approval import FileApprovalStore
from kingdee.client import KingdeeClient
from models.approval import PendingAction
from models.user import UserContext
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "key-alice"
_MANAGER_A_KEY = "key-manager-a"
_MANAGER_B_KEY = "key-manager-b"
_ADMIN_KEY = "key-admin"
_VIEWER_KEY = "key-viewer"

_ALICE = {"user_id": "alice", "role": "salesperson", "data_scope": {}}
_MANAGER_A = {"user_id": "mgr-a", "role": "manager", "data_scope": {}}
_MANAGER_B = {"user_id": "mgr-b", "role": "manager", "data_scope": {}}
_ADMIN = {"user_id": "admin", "role": "admin", "data_scope": {}}
_VIEWER = {"user_id": "viewer", "role": "viewer", "data_scope": {}}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {
            _ALICE_KEY: _ALICE,
            _MANAGER_A_KEY: _MANAGER_A,
            _MANAGER_B_KEY: _MANAGER_B,
            _ADMIN_KEY: _ADMIN,
            _VIEWER_KEY: _VIEWER,
        },
    )


def _app(settings: Settings, client: KingdeeClient) -> FastAPI:
    return create_app(settings, provider=FakeProvider(), client=client)


async def _create_action(app: FastAPI, *, key: str = "idem-api-1") -> PendingAction:
    deps: AppDeps = app.state.deps
    return await deps.approvals.create(
        trace_id="trace-api",
        session_id="sess-api",
        requester=UserContext(user_id="alice", role="salesperson"),
        tool_name="kingdee_update_sales_order",
        arguments={"bill_no": "SO000001", "amount": 120000},
        summary="修改销售订单 SO000001，金额 120000",
        form_id="SAL_Order",
        business_key="SO000001",
        risk_score=62.0,
        risk_level="dual_approve",
        required_approvers=["sales_manager"],
        idempotency_key=key,
    )


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


async def test_inbox_and_mine_scopes(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        await _create_action(app)
        manager = await http.get("/v1/approvals?scope=inbox", headers=_headers(_MANAGER_A_KEY))
        viewer = await http.get("/v1/approvals?scope=inbox", headers=_headers(_VIEWER_KEY))
        mine = await http.get("/v1/approvals?scope=mine", headers=_headers(_ALICE_KEY))

    assert manager.status_code == 200
    assert len(manager.json()["approvals"]) == 1
    card = manager.json()["approvals"][0]
    assert card["can_decide"] is True
    # 未登记的工具名回退原值，风险档位与角色展示中文
    assert card["tool_label"] == "kingdee_update_sales_order"
    assert card["risk_level_label"] == "双人复核"
    assert card["requester_role_label"] == "销售人员"
    assert viewer.status_code == 200
    assert viewer.json()["approvals"] == []
    assert len(mine.json()["approvals"]) == 1
    assert mine.json()["approvals"][0]["can_decide"] is False


async def test_all_scope_is_admin_only(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        await _create_action(app)
        admin = await http.get("/v1/approvals?scope=all", headers=_headers(_ADMIN_KEY))
        manager = await http.get("/v1/approvals?scope=all", headers=_headers(_MANAGER_A_KEY))

    assert admin.status_code == 200
    assert len(admin.json()["approvals"]) == 1
    assert manager.status_code == 403
    assert manager.json()["error"]["code"] == "approval_forbidden"


async def test_two_managers_approve(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        action = await _create_action(app)
        url = f"/v1/approvals/{action.approval_id}/approve"
        first = await http.post(
            url, headers=_headers(_MANAGER_A_KEY), json={"comment": "金额已核对"}
        )
        second = await http.post(url, headers=_headers(_MANAGER_B_KEY), json={})

    assert first.status_code == 200
    assert first.json()["status"] == "pending"
    assert len(first.json()["decisions"]) == 1
    assert second.status_code == 200
    assert second.json()["status"] == "approved"
    assert len(second.json()["decisions"]) == 2
    assert "arguments" not in second.text
    assert "idempotency_key" not in second.text


async def test_self_approval_and_viewer_forbidden(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        action = await _create_action(app)
        url = f"/v1/approvals/{action.approval_id}/approve"
        self_approve = await http.post(url, headers=_headers(_ALICE_KEY), json={})
        viewer = await http.post(url, headers=_headers(_VIEWER_KEY), json={})

    assert self_approve.status_code == 403
    assert self_approve.json()["error"]["code"] == "approval_forbidden"
    assert viewer.status_code == 403
    assert viewer.json()["error"]["code"] == "approval_forbidden"


async def test_reject_is_terminal(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        action = await _create_action(app)
        rejected = await http.post(
            f"/v1/approvals/{action.approval_id}/reject",
            headers=_headers(_MANAGER_A_KEY),
            json={"comment": "金额不对"},
        )
        conflict = await http.post(
            f"/v1/approvals/{action.approval_id}/approve",
            headers=_headers(_MANAGER_B_KEY),
            json={},
        )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "approval_conflict"


async def test_missing_approval_returns_404(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        response = await http.get("/v1/approvals/missing", headers=_headers(_MANAGER_A_KEY))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "approval_not_found"


async def test_expired_approval_returns_409(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    now = datetime.now(UTC)
    expired = PendingAction(
        approval_id="expired1",
        trace_id="trace-expired",
        requester_id="alice",
        requester_role="salesperson",
        tool_name="kingdee_update_sales_order",
        summary="过期单",
        idempotency_key="expired-key",
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    FileApprovalStore(settings.approval_store_dir).save(expired)
    app = _app(settings, client)

    async with api_client(app) as http:
        response = await http.post(
            "/v1/approvals/expired1/approve",
            headers=_headers(_MANAGER_A_KEY),
            json={},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_expired"


async def test_detail_visibility(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        action = await _create_action(app)
        url = f"/v1/approvals/{action.approval_id}"
        requester = await http.get(url, headers=_headers(_ALICE_KEY))
        manager = await http.get(url, headers=_headers(_MANAGER_A_KEY))
        viewer = await http.get(url, headers=_headers(_VIEWER_KEY))

    assert requester.status_code == 200
    assert manager.status_code == 200
    assert viewer.status_code == 404
    assert viewer.json()["error"]["code"] == "approval_not_found"


async def test_idempotent_approval(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        action = await _create_action(app)
        url = f"/v1/approvals/{action.approval_id}/approve"
        first = await http.post(url, headers=_headers(_MANAGER_A_KEY), json={})
        second = await http.post(url, headers=_headers(_MANAGER_A_KEY), json={})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "pending"
    assert len(second.json()["decisions"]) == 1
