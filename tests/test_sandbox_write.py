"""M9-T13：沙箱写端点契约测试（Save/Submit/Audit/UnAudit/Delete）。"""

import hashlib
import threading
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from sandbox.kingdee_stub.app import (
    AUDIT_PATH,
    DELETE_PATH,
    LOGIN_PATH,
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
from sandbox.kingdee_stub.store import reset_dataset, sales_order_store

_TIMESTAMP = 1_760_000_000
_SAVE_BODY = {
    "FCustomerID": {"FNumber": "CUS001"},
    "FMaterialID": {"FNumber": "MAT001"},
    "FQty": 10,
    "FTaxPrice": 100,
    "FNote": "契约测试",
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
    """已建立会话的客户端。"""
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
    response = client.post(SAVE_PATH, json={"parameters": ["SAL_Order", {"Model": body}]})
    return cast(dict[str, Any], response.json())


def _action(client: TestClient, path: str, bill_no: str) -> dict[str, Any]:
    response = client.post(path, json={"parameters": ["SAL_Order", {"Numbers": [bill_no]}]})
    return cast(dict[str, Any], response.json())


def _status(client: TestClient, bill_no: str) -> str | None:
    response = client.post(
        QUERY_PATH,
        json={
            "data": {
                "FormId": "SAL_Order",
                "FieldKeys": "FBillNo,FDocumentStatus,FBillAllAmount",
                "FilterString": f"FBillNo = '{bill_no}'",
                "OrderString": "FBillNo",
                "StartRow": 0,
                "Limit": 1,
            }
        },
    )
    rows = response.json()
    return str(rows[0][1]) if rows else None


def test_save_creates_draft_order(authed: TestClient) -> None:
    body = _save(authed)

    assert body["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert body["Result"]["Number"] == "SO900001"
    assert _status(authed, "SO900001") == "Z"
    order = sales_order_store.get("SO900001")
    assert order is not None
    assert order.bill_all_amount == "1000.0000000000"
    assert order.entries[0].material_number == "MAT001"


def test_save_accepts_guard_rewritten_fids(authed: TestClient) -> None:
    """Guard 3 换码后传 FID；写端点必须能反查编码。"""
    body = _save(authed, FCustomerID={"FID": 2003}, FMaterialID={"FID": 3002})

    assert body["Result"]["ResponseStatus"]["IsSuccess"] is True
    order = sales_order_store.get(str(body["Result"]["Number"]))
    assert order is not None
    assert order.customer_number == "CUS003"
    assert order.entries[0].material_number == "MAT002"


def test_full_state_machine(authed: TestClient) -> None:
    bill_no = str(_save(authed)["Result"]["Number"])

    assert _action(authed, SUBMIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "B"
    assert _action(authed, AUDIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "C"
    assert _action(authed, UNAUDIT_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert _status(authed, bill_no) == "A"
    assert _action(authed, DELETE_PATH, bill_no)["Result"]["ResponseStatus"]["IsSuccess"] is True
    assert sales_order_store.get(bill_no) is None


@pytest.mark.parametrize(
    ("path", "bill_no"),
    [
        (SUBMIT_PATH, "SO000006"),  # B 不能提交
        (AUDIT_PATH, "SO000004"),  # Z 不能直接审核
        (UNAUDIT_PATH, "SO000004"),  # Z 不能反审核
        (DELETE_PATH, "SO000001"),  # 已审核不能删除
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


def test_unknown_entities_are_rejected(authed: TestClient) -> None:
    unknown_customer = _save(authed, FCustomerID={"FNumber": "CUS999"})
    unknown_material = _save(authed, FMaterialID={"FNumber": "MAT999"})

    assert unknown_customer["Result"]["ResponseStatus"]["IsSuccess"] is False
    assert unknown_material["Result"]["ResponseStatus"]["IsSuccess"] is False
    assert sales_order_store.get("SO900001") is None


def test_missing_session_returns_msg_code(authed: TestClient) -> None:
    with TestClient(app) as anonymous:
        body = _save(anonymous)

    assert body["Result"]["ResponseStatus"]["MsgCode"] == 5
    assert body["Result"]["ResponseStatus"]["IsSuccess"] is False


def test_concurrent_creation_uses_unique_bill_numbers() -> None:
    """并发创建不重号（store 锁保护）。"""
    results: list[str] = []
    lock = threading.Lock()

    def create() -> None:
        order = sales_order_store.create(
            customer_number="CUS001",
            material_number="MAT001",
            quantity=1,
            tax_price=1,
        )
        with lock:
            results.append(order.bill_no)

    threads = [threading.Thread(target=create) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 20
    assert len(set(results)) == 20
