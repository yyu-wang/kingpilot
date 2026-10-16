"""M1-T02：金蝶协议模型单测。

fixture 全部照抄官方文档/实测报文原文（tests/fixtures/kingdee/），
确保协议形状与真金蝶一致——这是契约仿真策略的第一道锚点。
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from models.kingdee import (
    BillQueryParams,
    DocumentStatus,
    extract_response_status,
    rows_to_dicts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kingdee"
# 成功样例对应的查询列（官方实测原文）
FIELD_KEYS = "FBillNo,FDate,FCustomerID.FName,FDocumentStatus"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── 请求模型 BillQueryParams ──


def test_bill_query_params_dump_by_alias_uses_kingdee_keys() -> None:
    """序列化 by_alias=True 输出金蝶键名（client 发报文的路径）。"""
    params = BillQueryParams(form_id="SAL_Order", field_keys="FBillNo,FDate")
    dumped = params.model_dump(by_alias=True)
    assert dumped["FormId"] == "SAL_Order"
    assert dumped["FieldKeys"] == "FBillNo,FDate"
    assert dumped["Limit"] == 100  # 默认值随报文发出
    assert dumped["StartRow"] == 0


def test_bill_query_params_accepts_kingdee_keys_on_construct() -> None:
    """populate_by_name=True：用金蝶键名构造也可（解析外部传入的路径）。"""
    params = BillQueryParams.model_validate(
        {"FormId": "SAL_Order", "FieldKeys": "FBillNo", "Limit": 50}
    )
    assert params.form_id == "SAL_Order"
    assert params.limit == 50


def test_bill_query_params_rejects_invalid_values() -> None:
    """形状校验：空标识/负分页参数拒绝（fail-closed）。"""
    with pytest.raises(ValidationError):
        BillQueryParams(form_id="", field_keys="FBillNo")
    with pytest.raises(ValidationError):
        BillQueryParams(form_id="SAL_Order", field_keys="", limit=1)
    with pytest.raises(ValidationError):
        BillQueryParams(form_id="SAL_Order", field_keys="FBillNo", limit=-1)
    with pytest.raises(ValidationError):
        BillQueryParams(form_id="SAL_Order", field_keys="FBillNo", start_row=-1)


def test_bill_query_params_rejects_unknown_field() -> None:
    """extra="forbid"：拼写错误的字段不得静默通过。"""
    with pytest.raises(ValidationError):
        BillQueryParams.model_validate(
            {
                "FormId": "SAL_Order",
                "FieldKeys": "FBillNo",
                "PageSize": 100,
            }  # 方案 §3.1 的错误参数名
        )


# ── 多态响应判别 extract_response_status ──


def test_extract_status_success_fixture_returns_none() -> None:
    """官方成功样例（二维数组）：无业务错误。"""
    assert extract_response_status(_load("execute_bill_query_success.json")) is None


def test_extract_status_formid_error_fixture_parsed() -> None:
    """FormId 写错（MsgCode=4）：检出错误且消息为官方原文。"""
    status = extract_response_status(_load("execute_bill_query_error_formid.json"))
    assert status is not None
    assert status.is_success is False
    assert status.msg_code == 4
    assert "SAL_OutStockBill" in status.error_message


def test_extract_status_field_error_fixture_parsed() -> None:
    """字段名写错（MsgCode=9）：检出错误且消息为官方原文。"""
    status = extract_response_status(_load("execute_bill_query_error_field.json"))
    assert status is not None
    assert status.is_success is False
    assert "FNotAField" in status.error_message


def test_extract_status_empty_result_returns_none() -> None:
    """空结果 []：合法成功响应，不是错误。"""
    assert extract_response_status(_load("execute_bill_query_empty.json")) is None


# ── 行转换 rows_to_dicts ──


def test_rows_to_dicts_aligns_columns_and_keeps_null() -> None:
    """列对齐 = FieldKeys 顺序；取不到的值保留 None；关联字段键名原样。"""
    rows = _load("execute_bill_query_success.json")
    dicts = rows_to_dicts(FIELD_KEYS, rows)
    assert len(dicts) == 3
    first, second = dicts[0], dicts[1]
    assert first["FBillNo"] == " "
    assert first["FCustomerID.FName"] is None  # 取不到值就是 null（官方实测）
    assert first["FDocumentStatus"] == DocumentStatus.DRAFT.value
    assert second["FCustomerID.FName"] == "测试客户001"


def test_rows_to_dicts_rejects_column_mismatch() -> None:
    """行列数与 FieldKeys 不符 = 协议被破坏，fail-closed。"""
    with pytest.raises(ValueError, match="列数"):
        rows_to_dicts(FIELD_KEYS, [["only-one-col"]])


def test_rows_to_dicts_rejects_empty_key() -> None:
    """FieldKeys 含空列名（如连续逗号）：拒绝而非静默错位。"""
    with pytest.raises(ValueError, match="空列名"):
        rows_to_dicts("FBillNo,,FDate", [["a", "b", "c"]])


def test_rows_to_dicts_keeps_decimal_string_as_is() -> None:
    """数量 10 位小数字符串原样保留（决策 D4：精度转换归业务层，防 float 精度污染）。"""
    dicts = rows_to_dicts("FRealQty", [[2.0000000000], ["3.5000000000"]])
    assert dicts[0]["FRealQty"] == 2.0
    assert dicts[1]["FRealQty"] == "3.5000000000"  # 字符串原样，不做任何转换


# ── 枚举与容错 ──


def test_document_status_official_values() -> None:
    """FDocumentStatus 五值与官方文档一致（对接一般只取 C 已审核）。"""
    assert [s.value for s in DocumentStatus] == ["Z", "A", "B", "C", "D"]


def test_response_status_tolerates_missing_optional_blocks() -> None:
    """SuccessEntitys/SuccessMessages 缺省可解析（金蝶并不总回全字段）。"""
    raw = [[{"Result": {"ResponseStatus": {"IsSuccess": False, "Errors": []}}}]]
    status = extract_response_status(raw)
    assert status is not None
    assert status.success_entitys == []
