"""用友适配器骨架单测（M10-T09）。

骨架的验收点不是「能查到数据」，而是：
1. 实现 ``ErpClient`` 协议（``isinstance`` 通过）——证明「新增厂商 = 实现协议」成立；
2. 工厂按 ``erp_vendor`` 选中骨架；
3. 未接入时 ``query`` / ``invoke_action`` 明确失败，不返回伪装成功的空结果；
4. 端点描述符能独立组装中性查询请求（三步里的第 2 步）。
"""

from typing import Any

import pytest

from config.settings import Settings
from erp import BillQuerySpec, ErpClient, FilterExpression, eq
from erp.factory import build_erp_client
from erp.yonyou import PURCHASE_ORDER, YonyouClient, YonyouNotConfiguredError


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_skeleton_satisfies_erp_client_protocol() -> None:
    client = YonyouClient(_settings())

    assert isinstance(client, ErpClient)


def test_factory_selects_yonyou() -> None:
    client = build_erp_client(_settings(erp_vendor="yonyou"))

    assert isinstance(client, YonyouClient)
    assert isinstance(client, ErpClient)


async def test_context_manager_enters_and_exits() -> None:
    async with build_erp_client(_settings(erp_vendor="yonyou")) as client:
        assert isinstance(client, YonyouClient)


async def test_query_fails_closed_instead_of_returning_empty() -> None:
    client = YonyouClient(_settings())
    spec = PURCHASE_ORDER.to_query_params(limit=10)

    with pytest.raises(YonyouNotConfiguredError, match="未接入真实环境") as exc_info:
        await client.query(spec)

    assert exc_info.value.context["operation"] == "query"
    assert exc_info.value.context["form_id"] == "PU_PurchaseOrder"


async def test_invoke_action_fails_closed() -> None:
    client = YonyouClient(_settings())

    with pytest.raises(YonyouNotConfiguredError, match="未接入真实环境") as exc_info:
        await client.invoke_action("audit", "PU_PurchaseOrder", bill_no="PO001")

    assert exc_info.value.context["operation"] == "invoke_action"
    assert exc_info.value.context["action"] == "audit"


def test_endpoint_descriptor_builds_neutral_query() -> None:
    spec = PURCHASE_ORDER.to_query_params(
        filter=FilterExpression(conditions=(eq("cVenCode", "SUP001"),)),
        limit=20,
    )

    assert isinstance(spec, BillQuerySpec)
    assert spec.form_id == "PU_PurchaseOrder"
    assert spec.filter.conditions[0].field == "cVenCode"
    assert spec.limit == 20
    assert spec.order_by == "cCode"


def test_endpoint_keeps_viewable_not_filterable_separation() -> None:
    """``cMemo`` 可显示但不可过滤（两套字段白名单的最小示例）。"""
    assert "cMemo" in {field.name for field in PURCHASE_ORDER.fields}
    assert "cMemo" not in PURCHASE_ORDER.filterable_fields
