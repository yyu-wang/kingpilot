"""采购入库单沙箱写端点契约测试（Save/Submit/Audit/UnAudit/Delete）。"""

import hashlib
import threading
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from sandbox.kingdee_stub import reset_dataset
from sandbox.kingdee_stub.app import (
    AUDIT_PATH,
    DELETE_PATH,
    LOGIN_PATH,
    PURCHASE_INBOUND_FORM,
    QUERY_PATH,
    SANDBOX_ACCT_ID,
    SANDBOX_APP_ID,
    SANDBOX_APP_SECRET,
    SANDBOX_USER,
    SAVE_PATH,
    SUBMIT_PATH,
    UNAUDIT_PATH,
    app,
    reset_sessions,
)
from sandbox.kingdee_stub.inbound_store import purchase_inbound_store

_TIMESTAMP = 1_760_000_000
_SAVE_BODY = {
    "FSrcBillNo": "PO000001",
    "FSupplierID": {"FNumber": "SUP001"},
    "FMaterialID": {"FNumber": "MAT001"},
    "FStockId": {"FName": "成品仓"},
    "FRealQty": 50,
    "FTaxPrice": 10,
    "FDate": "2026-04-01",
    "FNote": "入库契约测试",
}


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    reset_sessions()
    reset_dataset()
    yield
    reset_sessions()
    reset_dataset()


@pytest.fixture
def authed() -> Iterator[TestClient]:
    with TestClient(app) as client:
        sign = hashlib.sha256(
            "".join(
                sorted(
                    [
                        SANDBOX_ACCT_ID,
                        SANDBOX_USER,
                        SANDBOX_APP_ID,
                        SANDBOX_APP_SECRET,
                        str(_TIMESTAMP),
                    ]
                )
            ).encode("utf-8")
        ).hexdigest()
        response = client.post(
            LOGIN_PATH,
            json={
                "parameters": [
                    SANDBOX_ACCT_ID,
                    SANDBOX_USER,
                    SANDBOX_APP_ID,
                    _TIMESTAMP,
                    sign,
                    2052,
                ]
            },
        )
        assert response.json()["LoginResultType"] == 1
        yield client


def _save(client: TestClient, **overrides: object) -> dict[str, Any]:
    body = {**_SAVE_BODY, **overrides}
    response = client.post(
        SAVE_PATH,
        json={"parameters": [PURCHASE_INBOUND_FORM, {"Model": body}]},
    )
    return cast(dict[str, Any], response.json())


def _action(client: TestClient, path: str, bill_no: str) -> dict[str, Any]:
    response = client.post(
        path,
        json={"parameters": [PURCHASE_INBOUND_FORM, {"Numbers": [bill_no]}]},
    )
    return cast(dict[str, Any], response.json())


def _status(client: TestClient, bill_no: str) -> str | None:
    response = client.post(
        QUERY_PATH,
        json={
            "data": {
                "FormId": PURCHASE_INBOUND_FORM,
                "FieldKeys": "FBillNo,FDocumentStatus,FRealQty",
                "FilterString": f"FBillNo = '{bill_no}'",
                "OrderString": "FBillNo",
                "StartRow": 0,
                "Limit": 1,
            }
        },
    )
    rows = response.json()
    return str(rows[0][1]) if rows else None


def test_save_creates_draft_inbound(authed: TestClient) -> None:
    body = _save(authed)

    assert body["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert body["Result"]["Number"] == "CGRK900001"
    row = purchase_inbound_store.get("CGRK900001")
    assert row is not None
    assert row.document_status == "Z"
    assert row.source_bill_no == "PO000001"
    assert row.supplier_number == "SUP001"
    assert row.material_number == "MAT001"
    assert row.warehouse_name == "成品仓"
    assert row.real_qty == "50.0000000000"
    assert row.amount == "500.0000000000"
    assert _status(authed, "CGRK900001") == "Z"


def test_save_accepts_guard_rewritten_fids(authed: TestClient) -> None:
    body = _save(
        authed,
        FSupplierID={"FID": 4001},
        FMaterialID={"FID": 3001},
        FSrcBillNo="PO000001",
    )

    assert body["Result"]["ResponseStatus"]["IsSuccess"] is True
    row = purchase_inbound_store.get(str(body["Result"]["Number"]))
    assert row is not None
    assert row.supplier_number == "SUP001"
    assert row.material_number == "MAT001"


def test_full_state_machine(authed: TestClient) -> None:
    bill_no = str(_save(authed)["Result"]["Number"])

    assert _action(authed, SUBMIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "B"
    assert _action(authed, AUDIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "C"
    assert _action(authed, UNAUDIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "A"
    assert _action(authed, DELETE_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert purchase_inbound_store.get(bill_no) is None


@pytest.mark.parametrize(
    ("path", "bill_no"),
    [
        (SUBMIT_PATH, "CGRK000001"),  # C 不能提交
        (AUDIT_PATH, "CGRK000004"),  # Z 不能直接审核
        (UNAUDIT_PATH, "CGRK000004"),  # Z 不能反审核
        (DELETE_PATH, "CGRK000001"),  # 已审核不能删除
    ],
)
def test_illegal_transition_returns_business_error(
    authed: TestClient, path: str, bill_no: str
) -> None:
    before = _status(authed, bill_no)
    body = _action(authed, path, bill_no)

    status = body["Result"]["ResponseStatus"]
    assert status["IsSuccess"] is False
    assert status["Errors"][0]["Message"]
    assert _status(authed, bill_no) == before


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"FSrcBillNo": "PO999999"}, "源采购订单不存在"),
        ({"FSrcBillNo": "PO000004"}, "源采购订单未审核"),
        ({"FSupplierID": {"FNumber": "SUP002"}}, "供应商与源采购订单不一致"),
        ({"FMaterialID": {"FNumber": "MAT999"}}, "物料不存在"),
        ({"FStockId": {"FName": "不存在的仓库"}}, "仓库不存在"),
        ({"FRealQty": 0}, "实收数量必须大于 0"),
    ],
)
def test_business_validation_rejects_and_creates_nothing(
    authed: TestClient, overrides: dict[str, object], message: str
) -> None:
    body = _save(authed, **overrides)

    status = body["Result"]["ResponseStatus"]
    assert status["IsSuccess"] is False
    assert message in status["Errors"][0]["Message"]
    assert purchase_inbound_store.get("CGRK900001") is None


def test_missing_session_returns_msg_code(authed: TestClient) -> None:
    with TestClient(app) as anonymous:
        body = _save(anonymous)

    assert body["Result"]["ResponseStatus"]["MsgCode"] == 5
    assert body["Result"]["ResponseStatus"]["IsSuccess"] is False


def test_query_sees_created_and_deleted_rows(authed: TestClient) -> None:
    bill_no = str(_save(authed)["Result"]["Number"])
    created = _status(authed, bill_no)
    assert created == "Z"

    _action(authed, DELETE_PATH, bill_no)
    assert _status(authed, bill_no) is None


def test_concurrent_creation_uses_unique_bill_numbers() -> None:
    results: list[str] = []
    lock = threading.Lock()

    def create() -> None:
        row = purchase_inbound_store.create(
            source_bill_no="PO000001",
            supplier_number="SUP001",
            material_number="MAT001",
            warehouse_name="成品仓",
            quantity=1,
        )
        with lock:
            results.append(row.bill_no)

    threads = [threading.Thread(target=create) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 20
    assert len(set(results)) == 20
