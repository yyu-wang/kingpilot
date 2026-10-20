"""M9-T05：/v1/chat 202、审批自动执行与手动恢复 API 测试。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from agent.tools import (
    ToolDefinition,
    ToolRegistry,
    build_default_registry,
)
from api.app import create_app
from api.deps import AppDeps
from config.settings import Settings
from governance.approval import ApprovalService
from governance.approval_executor import ApprovalExecution
from kingdee.client import KingdeeClient
from kingdee.endpoints.sal_order import SAL_ORDER, SalesOrderParams
from models.approval import PendingAction
from models.base import KingPilotBaseModel
from models.llm import PlanResult, ToolCallDecision, ToolSpec
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


def _no_filter(params: KingPilotBaseModel) -> str:
    return ""


def _write_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name="kingdee_delete_sales_order",
                description="删除销售订单（测试写工具）",
                parameters=SalesOrderParams.model_json_schema(),
            ),
            params_model=SalesOrderParams,
            build_filter=_no_filter,
            endpoint=SAL_ORDER,
            tool_type="action",
            action="delete",
            required_parameters=("bill_no",),
        )
    )
    return registry


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


def _provider() -> FakeProvider:
    return FakeProvider(
        plan_queue=[
            PlanResult(
                tool_calls=[
                    ToolCallDecision(
                        tool_name="kingdee_delete_sales_order",
                        arguments={"bill_no": "SO000001"},
                    )
                ]
            )
        ]
    )


class _FakeExecution:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.service: ApprovalService | None = None

    async def execute(self, approval_id: str) -> PendingAction:
        self.calls.append(approval_id)
        assert self.service is not None
        action = await self.service.get_for_execution(approval_id)
        return action.model_copy(
            update={
                "execution_status": "succeeded",
                "executed_at": datetime.now(UTC),
            }
        )


def _app(
    settings: Settings,
    client: KingdeeClient,
    *,
    execution: ApprovalExecution | None = None,
) -> FastAPI:
    return create_app(
        settings,
        provider=_provider(),
        client=client,
        registry=_write_registry(),
        approval_execution=execution,
    )


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


async def _pending_chat(http: httpx.AsyncClient) -> dict[str, Any]:
    response = await http.post(
        "/v1/chat",
        headers=_headers(_ALICE_KEY),
        json={"message": "删除销售订单 SO000001"},
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_chat_returns_202_with_approval_id(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        body = await _pending_chat(http)
        deps: AppDeps = app.state.deps
        action = await deps.approvals.get_for_execution(body["approval_id"])

    assert body["approval_pending"] is True
    assert body["approval_id"]
    assert action.status == "pending"
    assert action.execution_status == "not_started"
    assert action.form_id == "SAL_Order"
    assert action.business_key == "SO000001"


async def test_second_approval_auto_executes(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    execution = _FakeExecution()
    app = _app(_settings(tmp_path, sandbox_server), client, execution=execution)

    async with api_client(app) as http:
        execution.service = app.state.deps.approvals
        body = await _pending_chat(http)
        approval_id = body["approval_id"]
        url = f"/v1/approvals/{approval_id}/approve"
        first = await http.post(url, headers=_headers(_MANAGER_A_KEY), json={})
        second = await http.post(url, headers=_headers(_MANAGER_B_KEY), json={})

    assert first.status_code == 200
    assert first.json()["status"] == "pending"
    assert second.status_code == 200
    assert second.json()["status"] == "approved"
    assert second.json()["execution_status"] == "succeeded"
    assert execution.calls == [approval_id]


async def test_manual_execute_permissions(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    execution = _FakeExecution()
    app = _app(_settings(tmp_path, sandbox_server), client, execution=execution)

    async with api_client(app) as http:
        deps: AppDeps = app.state.deps
        execution.service = deps.approvals
        action = await deps.approvals.create(
            trace_id="trace-manual",
            session_id="sess-manual",
            requester=UserContext(user_id="alice", role="salesperson"),
            tool_name="kingdee_delete_sales_order",
            arguments={"bill_no": "SO000001"},
            summary="删除销售订单 SO000001",
            form_id="SAL_Order",
            business_key="SO000001",
            idempotency_key="manual-exec-1",
            write_action="delete",
        )
        await deps.approvals.approve(
            action.approval_id, UserContext(user_id="mgr-a", role="manager")
        )
        await deps.approvals.approve(
            action.approval_id, UserContext(user_id="mgr-b", role="manager")
        )
        url = f"/v1/approvals/{action.approval_id}/execute"
        viewer = await http.post(url, headers=_headers(_VIEWER_KEY))
        manager = await http.post(url, headers=_headers(_MANAGER_A_KEY))

    assert viewer.status_code == 403
    assert manager.status_code == 200
    assert manager.json()["execution_status"] == "succeeded"
    assert execution.calls == [action.approval_id]


async def test_normal_chat_still_200(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _settings(tmp_path, sandbox_server)
    app = create_app(
        settings,
        provider=FakeProvider(response_queue=["你好"]),
        client=client,
        registry=build_default_registry(),
    )

    async with api_client(app) as http:
        response = await http.post(
            "/v1/chat", headers=_headers(_ALICE_KEY), json={"message": "你好"}
        )

    assert response.status_code == 200
    assert response.json()["approval_pending"] is False
    assert response.json()["approval_id"] == ""
