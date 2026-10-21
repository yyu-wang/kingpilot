"""管理后台 API：总览、变更审计、测试数据集与静态页面。"""

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from api.app import create_app
from config.settings import Settings
from kingdee.client import KingdeeClient
from models.ledger import ProvenanceChain
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALICE_KEY = "admin-key-alice"
_VIEWER_KEY = "admin-key-viewer"
_MANAGER_KEY = "admin-key-manager"

_ALICE = {"user_id": "alice", "role": "admin", "data_scope": {}}
_VIEWER = {"user_id": "vicky", "role": "viewer", "data_scope": {}}
_MANAGER = {"user_id": "bob", "role": "manager", "data_scope": {}}

_EXPECTED_DATASET_ROWS = {
    "sales_orders": 130,
    "purchase_orders": 40,
    "purchase_inbounds": 14,
    "customers": 8,
    "suppliers": 5,
    "materials": 8,
}


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {_ALICE_KEY: _ALICE, _VIEWER_KEY: _VIEWER, _MANAGER_KEY: _MANAGER},
    )


def _app(settings: Settings, client: KingdeeClient) -> FastAPI:
    return create_app(settings, provider=FakeProvider(), client=client)


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _record_submit(
    app: FastAPI,
    *,
    business_key: str = "PO900001",
    timestamp: datetime | None = None,
) -> None:
    ledger = app.state.deps.resources_for("default").ledger
    ledger.record_write(
        trace_id=f"trace-admin-console-{business_key}",
        form_id="PUR_PurchaseOrder",
        business_key=business_key,
        action="submit",
        provenance=ProvenanceChain(
            user_original_input=f"提交采购订单 {business_key}",
            user_id="alice",
            approval_chain=["bob"],
            risk_score=33.2,
            risk_level="confirm",
            tool_call_params={"bill_no": business_key},
            api_response={"bill_no": business_key},
        ),
        before_snapshot={"FBillNo": business_key, "FDocumentStatus": "Z"},
        after_snapshot={"FBillNo": business_key, "FDocumentStatus": "B"},
        timestamp=timestamp,
    )


async def test_admin_requires_admin_role(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        viewer = await http.get("/v1/admin/overview", headers=_headers(_VIEWER_KEY))
        manager = await http.get("/v1/admin/overview", headers=_headers(_MANAGER_KEY))
        anonymous = await http.get("/v1/admin/overview")

    assert viewer.status_code == 403
    assert manager.status_code == 403
    assert anonymous.status_code == 401


async def test_admin_overview_counts_ledger_and_datasets(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        _record_submit(app)
        response = await http.get("/v1/admin/overview", headers=_headers(_ALICE_KEY))

    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["changes_total"] == 1
    assert body["changes_success"] == 1
    assert body["changes_failed"] == 0
    assert body["by_form"] == {"PUR_PurchaseOrder": 1}
    assert body["by_action"] == {"submit": 1}
    assert body["chain_valid"] is True
    for kind, expected in _EXPECTED_DATASET_ROWS.items():
        assert body["datasets"][kind] == expected
    assert body["datasets"]["inventory"] > 0


async def test_admin_dataset_covers_all_test_data(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        responses = {
            kind: await http.get(f"/v1/admin/datasets/{kind}", headers=_headers(_ALICE_KEY))
            for kind in (*_EXPECTED_DATASET_ROWS, "inventory")
        }

    for kind, response in responses.items():
        assert response.status_code == 200, f"{kind}: {response.text}"
        body: dict[str, Any] = response.json()
        assert body["kind"] == kind
        assert body["fields"]
        assert body["rows"], f"{kind} 应有测试数据"
        if kind in _EXPECTED_DATASET_ROWS:
            assert body["total"] == _EXPECTED_DATASET_ROWS[kind]

    sales = responses["sales_orders"].json()
    assert sales["fields"][0] == "FBillNo"
    assert any(row["FBillNo"] == "SO000001" for row in sales["rows"])

    # 单据类数据集按业务日期倒序：最新在最前（同日按单号倒序）
    for kind in ("sales_orders", "purchase_orders", "purchase_inbounds"):
        rows = responses[kind].json()["rows"]
        dates = [row["FDate"] for row in rows]
        assert dates == sorted(dates, reverse=True), kind
        same_day = [row["FBillNo"] for row in rows if row["FDate"] == dates[0]]
        assert same_day == sorted(same_day, reverse=True), kind

    purchase_orders = responses["purchase_orders"].json()
    anchor = next(row for row in purchase_orders["rows"] if row["FBillNo"] == "PO000002")
    assert anchor["FSupplierID.FNumber"] == "SUP002"
    assert anchor["FAmount"] == "160000.0000000000"

    inbounds = responses["purchase_inbounds"].json()
    assert any(
        row["FBillNo"] == "CGRK000001" and row["FSrcBillNo"] == "PO000001"
        for row in inbounds["rows"]
    )


async def test_admin_dataset_keyword_filter(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        response = await http.get(
            "/v1/admin/datasets/purchase_orders",
            headers=_headers(_ALICE_KEY),
            params={"q": "SUP004"},
        )

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["total"] >= 1
    assert {row["FSupplierID.FNumber"] for row in body["rows"]} == {"SUP004"}


async def test_admin_unknown_dataset_returns_404(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        response = await http.get("/v1/admin/datasets/unknown", headers=_headers(_ALICE_KEY))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "dataset_not_found"


async def test_admin_changes_include_snapshots_and_filters(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        _record_submit(app)
        matched = await http.get(
            "/v1/admin/changes",
            headers=_headers(_ALICE_KEY),
            params={"form_id": "PUR_PurchaseOrder", "action": "submit", "q": "PO900001"},
        )
        wrong_status = await http.get(
            "/v1/admin/changes",
            headers=_headers(_ALICE_KEY),
            params={"status": "failed"},
        )

    assert matched.status_code == 200, matched.text
    body: dict[str, Any] = matched.json()
    assert body["total"] == 1
    assert body["chain_valid"] is True
    item = body["items"][0]
    assert item["business_key"] == "PO900001"
    assert item["user_id"] == "alice"
    assert item["approval_chain"] == ["bob"]
    assert item["before_snapshot"]["FDocumentStatus"] == "Z"
    assert item["after_snapshot"]["FDocumentStatus"] == "B"
    assert item["field_diff"] == [
        {"field_name": "FDocumentStatus", "old_value": "Z", "new_value": "B"}
    ]
    assert wrong_status.json()["total"] == 0


async def test_admin_changes_sorted_by_timestamp_desc(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    """变更记录按时间倒序，与写入顺序无关。"""
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        # 先写较新的，再写较旧的：若按插入顺序倒排，旧记录会错误地排在前面
        _record_submit(app, business_key="PO900003", timestamp=datetime(2026, 4, 2))
        _record_submit(app, business_key="PO900002", timestamp=datetime(2026, 4, 1))
        response = await http.get("/v1/admin/changes", headers=_headers(_ALICE_KEY))

    assert response.status_code == 200, response.text
    assert [item["business_key"] for item in response.json()["items"]] == [
        "PO900003",
        "PO900002",
    ]


async def test_admin_static_page_and_assets(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        index = await http.get("/admin/")
        script = await http.get("/admin/admin.js")
        styles = await http.get("/admin/admin.css")

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "管理后台" in index.text
    assert "Content-Security-Policy" in index.headers
    assert script.status_code == 200
    assert styles.status_code == 200


async def test_admin_api_rejects_missing_key(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    async with api_client(app) as http:
        response = await http.get("/v1/admin/changes")

    assert response.status_code == 401
