"""M9-T13：销售订单完整写流程（API 审批 → 执行 → 快照/账本）。"""

from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from agent.tools import build_default_registry
from api.app import create_app
from api.deps import AppDeps
from config.settings import Settings
from kingdee.client import KingdeeClient
from models.llm import PlanResult, ToolCallDecision
from sandbox.kingdee_stub.store import sales_order_store
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "key-alice"
_BOB_KEY = "key-bob"
_CAROL_KEY = "key-carol"

_ALICE = {"user_id": "alice", "role": "salesperson", "data_scope": {}}
_BOB = {"user_id": "bob", "role": "manager", "data_scope": {}}
_CAROL = {"user_id": "carol", "role": "manager", "data_scope": {}}

_CREATE_ARGS = {
    "customer_number": "CUS001",
    "material_number": "MAT001",
    "quantity": 10,
    "tax_price": 100,
    "note": "端到端测试",
}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {_ALICE_KEY: _ALICE, _BOB_KEY: _BOB, _CAROL_KEY: _CAROL},
    )


def _plan(tool_name: str, arguments: dict[str, Any]) -> PlanResult:
    return PlanResult(tool_calls=[ToolCallDecision(tool_name=tool_name, arguments=arguments)])


def _provider() -> FakeProvider:
    return FakeProvider(
        plan_queue=[
            _plan("erp_create_sales_order", _CREATE_ARGS),
            _plan("erp_submit_sales_order", {"bill_no": "SO900001"}),
            _plan("erp_audit_sales_order", {"bill_no": "SO900001"}),
            _plan("erp_unaudit_sales_order", {"bill_no": "SO900001"}),
            _plan("erp_delete_sales_order", {"bill_no": "SO900001"}),
            _plan("erp_create_sales_order", _CREATE_ARGS),
        ]
    )


def _app(settings: Settings, client: KingdeeClient) -> FastAPI:
    return create_app(
        settings,
        provider=_provider(),
        client=client,
        registry=build_default_registry(),
    )


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


async def _chat(http: httpx.AsyncClient, key: str, message: str) -> dict[str, Any]:
    response = await http.post("/v1/chat", headers=_headers(key), json={"message": message})
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    assert body["approval_pending"] is True
    return body


async def _approve(http: httpx.AsyncClient, approval_id: str, key: str) -> dict[str, Any]:
    response = await http.post(
        f"/v1/approvals/{approval_id}/approve", headers=_headers(key), json={}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_sales_order_write_flow_end_to_end(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        deps: AppDeps = app.state.deps

        # 1) 创建：confirm 档 → 单票审批
        created = await _chat(http, _ALICE_KEY, "帮我创建一张销售订单")
        action = await deps.approvals.get_for_execution(created["approval_id"])
        assert action.required_approvals == 1
        assert action.business_key.startswith("CREATE-")
        assert action.execution_status == "not_started"

        approved = await _approve(http, created["approval_id"], _BOB_KEY)
        assert approved["status"] == "approved"
        assert approved["execution_status"] == "succeeded"
        assert sales_order_store.get("SO900001") is not None
        assert sales_order_store.get("SO900001").document_status == "Z"  # type: ignore[union-attr]

        create_records = deps.resources_for("default").ledger.find_by_trace_id(created["trace_id"])
        assert len(create_records) == 1
        assert create_records[0].action == "save"
        assert create_records[0].status == "success"
        assert create_records[0].provenance.api_response["bill_no"] == "SO900001"

        # 重复执行不重复写
        again = await http.post(
            f"/v1/approvals/{created['approval_id']}/execute", headers=_headers(_BOB_KEY)
        )
        assert again.status_code == 200
        assert len(deps.resources_for("default").ledger.find_by_trace_id(created["trace_id"])) == 1

        # 2) 提交：Z → B
        submitted = await _chat(http, _ALICE_KEY, "提交销售订单 SO900001")
        bob = await _approve(http, submitted["approval_id"], _BOB_KEY)
        assert bob["status"] == "approved"
        assert bob["execution_status"] == "succeeded"
        order = sales_order_store.get("SO900001")
        assert order is not None and order.document_status == "B"
        submit_record = deps.resources_for("default").ledger.find_by_trace_id(
            submitted["trace_id"]
        )[0]
        assert submit_record.business_key == "SO900001"
        assert submit_record.after_snapshot is not None
        assert submit_record.after_snapshot["FDocumentStatus"] == "B"

        # 3) 审核：B → C，双票
        audited = await _chat(http, _ALICE_KEY, "审核销售订单 SO900001")
        audit_action = await deps.approvals.get_for_execution(audited["approval_id"])
        assert audit_action.required_approvals == 2

        first = await _approve(http, audited["approval_id"], _BOB_KEY)
        assert first["status"] == "pending"
        assert first["execution_status"] == "not_started"
        assert sales_order_store.get("SO900001").document_status == "B"  # type: ignore[union-attr]

        second = await _approve(http, audited["approval_id"], _CAROL_KEY)
        assert second["status"] == "approved"
        assert second["execution_status"] == "succeeded"
        assert sales_order_store.get("SO900001").document_status == "C"  # type: ignore[union-attr]

        # 4) 反审核：C → A
        unaudited = await _chat(http, _ALICE_KEY, "反审核销售订单 SO900001")
        await _approve(http, unaudited["approval_id"], _BOB_KEY)
        await _approve(http, unaudited["approval_id"], _CAROL_KEY)
        assert sales_order_store.get("SO900001").document_status == "A"  # type: ignore[union-attr]

        # 5) 删除：A → 删除
        deleted = await _chat(http, _ALICE_KEY, "删除销售订单 SO900001")
        await _approve(http, deleted["approval_id"], _BOB_KEY)
        await _approve(http, deleted["approval_id"], _CAROL_KEY)
        assert sales_order_store.get("SO900001") is None
        delete_record = deps.resources_for("default").ledger.find_by_trace_id(deleted["trace_id"])[
            0
        ]
        assert delete_record.action == "delete"
        assert delete_record.before_snapshot is not None
        assert delete_record.before_snapshot["FDocumentStatus"] == "A"
        assert delete_record.after_snapshot is None

        # 6) 拒绝 → 零执行
        rejected_chat = await _chat(http, _ALICE_KEY, "再创建一张销售订单")
        rejected = await http.post(
            f"/v1/approvals/{rejected_chat['approval_id']}/reject",
            headers=_headers(_BOB_KEY),
            json={"comment": "不允许"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["execution_status"] == "not_started"
        assert sales_order_store.get("SO900002") is None
        assert (
            deps.resources_for("default").ledger.find_by_trace_id(rejected_chat["trace_id"]) == []
        )

        verification = deps.resources_for("default").ledger.verify()
        assert verification.valid is True
