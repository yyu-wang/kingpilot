"""M1-T06a：仿真沙箱查询（ExecuteBillQuery）端点单测。

锚点：用生产侧解析器 ``extract_response_status`` 反解仿真端的错误信封——
这一步同时验证了两件事：仿真端产出的失败报文形状正确，且生产解析器能吃它。
这是「契约替身」相对「函数 mock」的核心价值：形状错了就会红。
"""

import hashlib
from collections.abc import Iterator
from decimal import Decimal
from typing import cast

import pytest
from fastapi.testclient import TestClient

from models.kingdee import ResponseStatus, extract_response_status
from sandbox.kingdee_stub.app import (
    LOGIN_PATH,
    QUERY_PATH,
    SANDBOX_ACCT_ID,
    SANDBOX_APP_ID,
    SANDBOX_APP_SECRET,
    SANDBOX_USER,
    app,
    reset_sessions,
)

_TIMESTAMP = 1_760_000_000
_BILL_NO_FIELD = "FBillNo"


@pytest.fixture(autouse=True)
def _clean_sessions() -> Iterator[None]:
    reset_sessions()
    yield
    reset_sessions()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed(client: TestClient) -> TestClient:
    """已建立会话的客户端（Query 端点必须带 Cookie 才可用）。"""
    secret = SANDBOX_APP_SECRET
    sign = hashlib.sha256(
        "".join(
            sorted([SANDBOX_ACCT_ID, SANDBOX_USER, SANDBOX_APP_ID, secret, str(_TIMESTAMP)])
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
    return client


def _query(
    client: TestClient,
    *,
    form_id: str = "SAL_Order",
    field_keys: str = "FBillNo,FDate",
    filter_string: str = "",
    order_string: str = "",
    start_row: int = 0,
    limit: int = 100,
    top_row_count: int = 0,
) -> list[list[object]]:
    response = client.post(
        QUERY_PATH,
        json={
            "data": {
                "FormId": form_id,
                "FieldKeys": field_keys,
                "FilterString": filter_string,
                "OrderString": order_string,
                "TopRowCount": top_row_count,
                "StartRow": start_row,
                "Limit": limit,
            }
        },
    )
    assert response.status_code == 200
    return cast("list[list[object]]", response.json())


def _error_status(client: TestClient, **kwargs: object) -> ResponseStatus:
    """执行必定失败的查询，返回生产解析器解出的错误状态块。"""
    raw = client.post(
        QUERY_PATH,
        json={
            "data": {
                "FormId": "SAL_Order",
                "FieldKeys": _BILL_NO_FIELD,
                **kwargs,
            }
        },
    ).json()
    status = extract_response_status(raw)
    assert status is not None, f"期望错误信封，实际: {raw!r}"
    assert status.is_success is False
    return status


def test_query_returns_columns_in_field_keys_order(authed: TestClient) -> None:
    rows = _query(authed, field_keys="FBillNo,FDocumentStatus,FBillAllAmount", limit=3)

    assert len(rows) == 3
    assert rows[0] == ["SO000001", "C", "150000.0000000000"]


def test_query_first_page_is_capped_by_limit(authed: TestClient) -> None:
    rows = _query(authed, field_keys=_BILL_NO_FIELD, limit=100)

    assert len(rows) == 100


def test_query_second_page_returns_remaining_rows(authed: TestClient) -> None:
    rows = _query(authed, field_keys=_BILL_NO_FIELD, start_row=100, limit=100)

    assert len(rows) == 30
    assert rows[0][0] == "SO000101"


def test_query_limit_zero_returns_all_rows(authed: TestClient) -> None:
    rows = _query(authed, field_keys=_BILL_NO_FIELD, limit=0)

    assert len(rows) == 130


def test_query_all_amounts_are_decimal_strings(authed: TestClient) -> None:
    rows = _query(authed, field_keys="FBillAllAmount", limit=5)

    assert all(isinstance(row[0], str) and row[0].endswith("0000000000") for row in rows)


def test_query_filter_by_document_status(authed: TestClient) -> None:
    rows = _query(
        authed,
        field_keys="FBillNo,FDocumentStatus",
        filter_string="FDocumentStatus = 'Z'",
        limit=0,
    )

    assert rows
    assert {row[1] for row in rows} == {"Z"}
    assert "SO000004" in {row[0] for row in rows}


def test_query_numeric_filter_compares_numerically(authed: TestClient) -> None:
    """``> 100000`` 必须按数值比较（字符串比较会得出 "45000" > "100000" 的错误结论）。"""
    rows = _query(
        authed,
        field_keys=_BILL_NO_FIELD,
        filter_string="FBillAllAmount > 100000",
        limit=0,
    )

    bill_numbers = {row[0] for row in rows}
    assert "SO000001" in bill_numbers  # 150000
    assert "SO000008" not in bill_numbers  # 45000


def test_query_like_filter_matches_bill_number_pattern(authed: TestClient) -> None:
    rows = _query(authed, field_keys=_BILL_NO_FIELD, filter_string="FBillNo LIKE '%0000%'", limit=0)

    assert rows
    assert all("0000" in str(row[0]) for row in rows)


def test_query_filter_field_absent_from_field_keys_is_still_applied(authed: TestClient) -> None:
    """真金蝶允许过滤字段不出现在 FieldKeys——替身必须复现，否则会放过假绿。"""
    rows = _query(authed, field_keys=_BILL_NO_FIELD, filter_string="FDocumentStatus = 'C'", limit=0)

    bill_numbers = {row[0] for row in rows}
    assert all(len(row) == 1 for row in rows)
    assert "SO000001" in bill_numbers
    assert "SO000004" not in bill_numbers  # 该单为暂存状态 Z


def test_query_entry_field_expands_one_order_into_multiple_rows(authed: TestClient) -> None:
    rows = _query(authed, field_keys="FBillNo,FQty", limit=0)

    first_order_rows = [row for row in rows if row[0] == "SO000001"]
    assert len(first_order_rows) == 3  # SO000001 含 3 条明细
    assert len(rows) > 130  # 展开后总行数多于单据数


def test_query_order_by_amount_descending(authed: TestClient) -> None:
    rows = _query(
        authed,
        field_keys="FBillAllAmount",
        order_string="FBillAllAmount DESC",
        limit=0,
    )

    amounts = [Decimal(str(row[0])) for row in rows]
    assert amounts == sorted(amounts, reverse=True)


def test_query_unauthenticated_request_is_rejected(client: TestClient) -> None:
    status = _error_status(client)

    assert status.msg_code == 5
    assert "会话" in status.error_message


def test_query_unknown_form_id_is_rejected(authed: TestClient) -> None:
    status = _error_status(authed, FormId="SAL_Bogus")

    assert "不存在" in status.error_message


def test_query_unknown_field_is_rejected(authed: TestClient) -> None:
    status = _error_status(authed, FieldKeys="FBillNo,FBogus")

    assert "FBogus" in status.error_message


def test_query_filter_on_non_filterable_field_is_rejected(authed: TestClient) -> None:
    """``FCustomerID.FName`` 可显示不可过滤——白名单必须区分这两个集合。"""
    status = _error_status(authed, FilterString="FCustomerID.FName = '测试客户001'")

    assert "FCustomerID.FName" in status.error_message


def test_query_unsupported_filter_syntax_is_rejected(authed: TestClient) -> None:
    """仿真端不支持 OR 分组时宁可报错，也不静默忽略（静默忽略会制造假绿）。"""
    status = _error_status(authed, FilterString="FDocumentStatus = 'C' OR FDocumentStatus = 'B'")

    assert "OR" in status.error_message


def test_query_ordering_by_entry_field_is_rejected(authed: TestClient) -> None:
    status = _error_status(authed, OrderString="FQty")

    assert "FQty" in status.error_message


def test_query_top_row_count_is_rejected_as_unimplemented(authed: TestClient) -> None:
    status = _error_status(authed, TopRowCount=1)

    assert "TopRowCount" in status.error_message


# ── STK_Inventory 端点（M2-T04：注册表化后新增的业务对象）──


def test_inventory_query_filters_by_material(authed: TestClient) -> None:
    rows = _query(
        authed,
        form_id="STK_Inventory",
        field_keys="FMaterialId.FNumber,FMaterialId.FName,FStockId.FName,FBaseQty",
        filter_string="FMaterialId.FNumber = 'MAT001'",
    )

    numbers = {row[0] for row in rows}
    assert numbers == {"MAT001"}  # 锚点：分布在成品仓 + 原料仓两行
    assert len(rows) == 2
    assert rows[0][3] == "100.0000000000"  # 列序严格 = FieldKeys 序
    assert rows[1][3] == "50.0000000000"


def test_inventory_query_rejects_unknown_field(authed: TestClient) -> None:
    """库存端点有自己的字段白名单——销售订单字段不可混用（fail-closed）。"""
    status = _error_status(authed, FormId="STK_Inventory", FieldKeys="FBillNo")

    assert "FBillNo" in status.error_message


def test_inventory_query_rejects_non_filterable_field(authed: TestClient) -> None:
    status = _error_status(
        authed, FormId="STK_Inventory", FieldKeys="FBaseQty", FilterString="FBaseQty > '50'"
    )

    assert "FBaseQty" in status.error_message
