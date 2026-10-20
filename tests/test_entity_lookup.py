"""M4-T04/T06：KingdeeEntityLookup 单测（fake client，零网络）。

覆盖解析顺序：内码 FID → 业务编号 → 名称精确 → 模糊候选；以及歧义、
非法标识、不支持的实体类型、allow_name=False 等边界。
"""

from typing import cast

import pytest

from infrastructure.exceptions import KingdeeError
from kingdee.client import KingdeeClient
from kingdee.entity_lookup import KingdeeEntityLookup
from models.kingdee import BillQueryParams, BillQueryResult, JsonScalar


class _FakeClient:
    """按调用顺序返回预设行的假客户端。"""

    def __init__(self, rows: list[list[dict[str, JsonScalar]]]) -> None:
        self._rows = rows
        self.queries: list[BillQueryParams] = []

    async def execute_bill_query(self, params: BillQueryParams) -> BillQueryResult:
        self.queries.append(params)
        rows = self._rows.pop(0) if self._rows else []
        return BillQueryResult(
            form_id=params.form_id,
            field_keys=params.field_keys.split(","),
            rows=rows,
        )


def _lookup(client: _FakeClient) -> KingdeeEntityLookup:
    return KingdeeEntityLookup(cast(KingdeeClient, client))


async def test_exact_fid_hit() -> None:
    client = _FakeClient([[{"FID": "100001", "FNumber": "CUS001", "FName": "深圳XX公司"}]])

    result = await _lookup(client).lookup("customer", "100001")

    assert result.found is True
    assert result.canonical_id == "100001"
    assert result.number == "CUS001"
    assert len(client.queries) == 1
    assert client.queries[0].filter_string == "FID = 100001"


async def test_exact_code_hit() -> None:
    client = _FakeClient([[{"FID": "100001", "FNumber": "CUS001", "FName": "深圳XX公司"}]])

    result = await _lookup(client).lookup("customer", "CUS001")

    assert result.found is True
    assert result.canonical_id == "100001"
    assert result.display_name == "深圳XX公司"
    assert result.number == "CUS001"
    assert len(client.queries) == 1
    query = client.queries[0]
    assert query.filter_string == "FNumber = 'CUS001'"
    assert query.field_keys == "FID,FNumber,FName"
    assert query.limit == 1


async def test_exact_name_hit_when_number_misses() -> None:
    client = _FakeClient(
        [
            [],
            [{"FID": "100001", "FNumber": "CUS001", "FName": "深圳XX公司"}],
        ]
    )

    result = await _lookup(client).lookup("customer", "深圳XX公司")

    assert result.found is True
    assert result.canonical_id == "100001"
    assert len(client.queries) == 2
    assert client.queries[1].filter_string == "FName = '深圳XX公司'"
    assert client.queries[1].limit == 2


async def test_ambiguous_name_returns_candidates_without_fuzzy_query() -> None:
    client = _FakeClient(
        [
            [],
            [
                {"FID": "100001", "FNumber": "CUS001", "FName": "深圳XX公司"},
                {"FID": "100002", "FNumber": "CUS002", "FName": "深圳XX公司"},
            ],
        ]
    )

    result = await _lookup(client).lookup("customer", "深圳XX公司")

    assert result.found is False
    assert result.candidates == ("深圳XX公司（CUS001）", "深圳XX公司（CUS002）")
    assert len(client.queries) == 2


async def test_fuzzy_candidates_on_miss() -> None:
    client = _FakeClient(
        [
            [],
            [],
            [{"FID": "100009", "FNumber": "CUS009", "FName": "深圳XX科技"}],
        ]
    )

    result = await _lookup(client).lookup("customer", "深圳XX公司")

    assert result.found is False
    assert result.candidates == ("深圳XX科技（CUS009）",)
    assert len(client.queries) == 3
    assert client.queries[2].filter_string == "FName LIKE '%深圳XX公司%'"
    assert client.queries[2].limit == 3


async def test_order_uses_bill_number_only() -> None:
    client = _FakeClient([[{"FID": 100008, "FBillNo": "SO000008"}]])

    result = await _lookup(client).lookup("order", "SO000008")

    assert result.found is True
    assert result.canonical_id == "100008"
    assert result.number == "SO000008"
    assert client.queries[0].field_keys == "FID,FBillNo"


async def test_allow_name_false_skips_name_lookup() -> None:
    client = _FakeClient([[]])

    result = await _lookup(client).lookup("customer", "CUS404", allow_name=False)

    assert result.found is False
    assert len(client.queries) == 1


async def test_unsupported_entity_type_raises() -> None:
    client = _FakeClient([])

    with pytest.raises(KingdeeError, match="不支持的实体类型"):
        await _lookup(client).lookup("warehouse", "WH001")

    assert client.queries == []


async def test_illegal_identifier_raises_before_query() -> None:
    client = _FakeClient([])

    with pytest.raises(KingdeeError, match="非法字符"):
        await _lookup(client).lookup("customer", "O'Reilly")

    assert client.queries == []


async def test_blank_identifier_returns_not_found() -> None:
    client = _FakeClient([])

    result = await _lookup(client).lookup("customer", "   ")

    assert result.found is False
    assert client.queries == []
