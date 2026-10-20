"""采购订单沙箱写端点契约测试（Save/Submit/Audit/UnAudit/Delete）。"""

import hashlib
import threading
from collections.abc import Iterator
from datetime import date
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from sandbox.kingdee_stub import reset_dataset
from sandbox.kingdee_stub.app import (
    AUDIT_PATH,
    DELETE_PATH,
    LOGIN_PATH,
    PURCHASE_ORDER_FORM,
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
from sandbox.kingdee_stub.purchase_order_store import purchase_order_store

_TIMESTAMP = 1_760_000_000
_SAVE_BODY = {
    "FSupplierID": {"FNumber": "SUP001"},
    "FMaterialID": {"FNumber": "MAT001"},
    "FQty": 10,
    "FTaxPrice": 100,
    "FDate": "2026-04-01",
    "FNote": "下单契约测试",
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
        json={"parameters": [PURCHASE_ORDER_FORM, {"Model": body}]},
    )
    return cast(dict[str, Any], response.json())


def _action(client: TestClient, path: str, bill_no: str) -> dict[str, Any]:
    response = client.post(
        path,
        json={"parameters": [PURCHASE_ORDER_FORM, {"Numbers": [bill_no]}]},
    )
    return cast(dict[str, Any], response.json())


def _status(client: TestClient, bill_no: str) -> str | None:
    response = client.post(
        QUERY_PATH,
        json={
            "data": {
                "FormId": PURCHASE_ORDER_FORM,
                "FieldKeys": "FBillNo,FDocumentStatus,FQty",
                "FilterString": f"FBillNo = '{bill_no}'",
                "OrderString": "FBillNo",
                "StartRow": 0,
                "Limit": 1,
            }
        },
    )
    rows = response.json()
    return str(rows[0][1]) if rows else None


def test_save_creates_draft_purchase_order(authed: TestClient) -> None:
    body = _save(authed)

    assert body["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert body["Result"]["Number"] == "PO900001"
    row = purchase_order_store.get("PO900001")
    assert row is not None
    assert row.document_status == "Z"
    assert row.supplier_number == "SUP001"
    assert row.supplier_name == "测试供应商001"
    assert row.material_number == "MAT001"
    assert row.material_name == "测试物料001"
    assert row.qty == "10.0000000000"
    assert row.tax_price == "100.0000000000"
    assert row.amount == "1000.0000000000"
    assert row.date == "2026-04-01"
    assert row.note == "下单契约测试"
    assert _status(authed, "PO900001") == "Z"


def test_save_accepts_guard_rewritten_fids(authed: TestClient) -> None:
    body = _save(
        authed,
        FSupplierID={"FID": 4001},
        FMaterialID={"FID": 3001},
    )

    assert body["Result"]["ResponseStatus"]["IsSuccess"] is True
    row = purchase_order_store.get(str(body["Result"]["Number"]))
    assert row is not None
    assert row.supplier_number == "SUP001"
    assert row.material_number == "MAT001"


def test_save_defaults_date_to_today(authed: TestClient) -> None:
    body = _save(authed, FDate="")

    assert body["Result"]["ResponseStatus"]["IsSuccess"] is True
    row = purchase_order_store.get(str(body["Result"]["Number"]))
    assert row is not None
    assert row.date == date.today().isoformat()


def test_full_state_machine(authed: TestClient) -> None:
    bill_no = str(_save(authed)["Result"]["Number"])

    assert _action(authed, SUBMIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "B"
    assert _action(authed, AUDIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "C"
    assert _action(authed, UNAUDIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "A"
    assert _action(authed, DELETE_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert purchase_order_store.get(bill_no) is None


@pytest.mark.parametrize(
    ("path", "bill_no"),
    [
        (SUBMIT_PATH, "PO000001"),  # C 不能提交
        (AUDIT_PATH, "PO000004"),  # Z 不能直接审核
        (UNAUDIT_PATH, "PO000004"),  # Z 不能反审核
        (DELETE_PATH, "PO000001"),  # 已审核不能删除
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
        ({"FSupplierID": {"FNumber": "SUP999"}}, "供应商不存在"),
        ({"FMaterialID": {"FNumber": "MAT999"}}, "物料不存在"),
        ({"FQty": 0}, "采购数量必须大于 0"),
        ({"FQty": -5}, "采购数量必须大于 0"),
        ({"FTaxPrice": 0}, "含税单价必须大于 0"),
        ({"FQty": "abc"}, "采购数量不是合法数字"),
    ],
)
def test_business_validation_rejects_and_creates_nothing(
    authed: TestClient, overrides: dict[str, object], message: str
) -> None:
    body = _save(authed, **overrides)

    status = body["Result"]["ResponseStatus"]
    assert status["IsSuccess"] is False
    assert message in status["Errors"][0]["Message"]
    assert purchase_order_store.get("PO900001") is None


def test_missing_session_returns_msg_code(authed: TestClient) -> None:
    with TestClient(app) as anonymous:
        body = _save(anonymous)

    assert body["Result"]["ResponseStatus"]["MsgCode"] == 5
    assert body["Result"]["ResponseStatus"]["IsSuccess"] is False


def test_query_sees_created_and_deleted_rows(authed: TestClient) -> None:
    bill_no = str(_save(authed)["Result"]["Number"])
    assert _status(authed, bill_no) == "Z"

    _action(authed, DELETE_PATH, bill_no)
    assert _status(authed, bill_no) is None


def test_concurrent_creation_uses_unique_bill_numbers() -> None:
    results: list[str] = []
    lock = threading.Lock()

    def create() -> None:
        row = purchase_order_store.create(
            supplier_number="SUP001",
            material_number="MAT001",
            quantity=1,
            tax_price=1,
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
