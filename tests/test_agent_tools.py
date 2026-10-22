"""M2-T03：Tool 定义框架单测（respx 零网络）。

分层：本文件验「工具层逻辑」（校验/组装/注册表/单一真源）；
端点组装格式见 test_endpoint_query_bindings.py；全链路见集成测试。
URL 常量复用沙箱 app 的公开定义——避免测试与实现各拼一份字符串
（M1-T06b 教训：同义反复常量会掩盖拼接错误）。
"""

import json

import pytest
import respx

from agent.tools import ToolDefinition, build_default_registry
from erp import FilterExpression
from infrastructure.exceptions import ToolError
from kingdee.client import KingdeeClient
from kingdee.endpoints.bd_customer import CUSTOMER_TOOL, CustomerParams
from kingdee.endpoints.sal_order import SAL_ORDER_TOOL, SalesOrderParams
from models.kingdee import BillQueryResult
from models.llm import ToolSpec
from sandbox import sandbox_settings
from sandbox.kingdee_stub.app import LOGIN_PATH, QUERY_PATH

_BASE_URL = "http://kd.test"
_LOGIN_URL = f"{_BASE_URL}{LOGIN_PATH}"
_QUERY_URL = f"{_BASE_URL}{QUERY_PATH}"
# 列序与 SAL_ORDER.default_field_keys 一致；值锚定 dataset 具名样例 SO000001
_MOCK_ROW = ["SO000001", "2026-01-05", "测试客户001", "C", "150000.0000000000"]

EXPECTED_TOOL_NAMES = {
    "erp_query_sales_order",
    "erp_query_purchase_order",
    "erp_query_purchase_inbound",
    "erp_query_customer",
    "erp_query_supplier",
    "erp_query_inventory",
    "erp_create_sales_order",
    "erp_submit_sales_order",
    "erp_audit_sales_order",
    "erp_unaudit_sales_order",
    "erp_delete_sales_order",
    "erp_create_purchase_inbound",
    "erp_submit_purchase_inbound",
    "erp_audit_purchase_inbound",
    "erp_unaudit_purchase_inbound",
    "erp_delete_purchase_inbound",
    "erp_create_purchase_order",
    "erp_submit_purchase_order",
    "erp_audit_purchase_order",
    "erp_unaudit_purchase_order",
    "erp_delete_purchase_order",
    "kb_search",
}


@respx.mock
async def test_execute_returns_typed_rows_and_sends_filter() -> None:
    """合法参数 → 组装进请求体 → 返回按键转换的 dict 行（锚点 SO000001）。"""
    respx.post(_LOGIN_URL).respond(json={"LoginResultType": 1})
    route = respx.post(_QUERY_URL).respond(json=[_MOCK_ROW])  # 金蝶成功响应 = 二维数组

    async with KingdeeClient(sandbox_settings(_BASE_URL)) as client:
        tool = build_default_registry().get("erp_query_sales_order")
        result = await tool.execute(client, {"limit": 5, "document_status": "C"})

    assert isinstance(result, BillQueryResult)
    assert result.row_count == 1
    assert result.rows[0]["FBillNo"] == "SO000001"
    assert result.rows[0]["FBillAllAmount"] == "150000.0000000000"  # 金额字符串原样
    body = json.loads(route.calls.last.request.content)
    assert body["data"]["FilterString"] == "FDocumentStatus = 'C'"
    assert body["data"]["Limit"] == 5
    assert body["data"]["FormId"] == "SAL_Order"


@respx.mock
async def test_execute_with_include_entries_requests_material_fields() -> None:
    """include_entries=true → FieldKeys 含明细字段，结果带回物料编码与名称。"""
    respx.post(_LOGIN_URL).respond(json={"LoginResultType": 1})
    route = respx.post(_QUERY_URL).respond(
        json=[
            [
                *_MOCK_ROW,
                "1",
                "MAT001",
                "测试物料001",
                "10.0000000000",
                "5000.0000000000",
                "2026-01-20",
            ]
        ]
    )

    async with KingdeeClient(sandbox_settings(_BASE_URL)) as client:
        tool = build_default_registry().get("erp_query_sales_order")
        result = await tool.execute(client, {"bill_no": "SO000001", "include_entries": True})

    body = json.loads(route.calls.last.request.content)
    field_keys = body["data"]["FieldKeys"].split(",")
    assert "FMaterialId.FNumber" in field_keys
    assert "FMaterialId.FName" in field_keys
    assert isinstance(result, BillQueryResult)
    assert result.rows[0]["FMaterialId.FNumber"] == "MAT001"
    assert result.rows[0]["FMaterialId.FName"] == "测试物料001"


async def test_execute_rejects_unknown_argument() -> None:
    """未知参数被 extra=forbid 拒绝且归一为 ToolError（校验先于任何网络请求）。"""
    async with KingdeeClient(sandbox_settings(_BASE_URL)) as client:
        tool = build_default_registry().get("erp_query_sales_order")
        with pytest.raises(ToolError, match="参数校验失败"):
            await tool.execute(client, {"bogus": 1})


async def test_execute_rejects_out_of_range_limit() -> None:
    """limit 越界（0 / 101）在参数层拒绝。"""
    async with KingdeeClient(sandbox_settings(_BASE_URL)) as client:
        tool = build_default_registry().get("erp_query_sales_order")
        with pytest.raises(ToolError, match="参数校验失败"):
            await tool.execute(client, {"limit": 0})
        with pytest.raises(ToolError, match="参数校验失败"):
            await tool.execute(client, {"limit": 101})


async def test_execute_rejects_invalid_enum_value() -> None:
    """非法单据状态在参数层拒绝（不进组装层）。"""
    async with KingdeeClient(sandbox_settings(_BASE_URL)) as client:
        tool = build_default_registry().get("erp_query_sales_order")
        with pytest.raises(ToolError, match="参数校验失败"):
            await tool.execute(client, {"document_status": "X"})


async def test_execute_wraps_filter_rejection_as_tool_error() -> None:
    """组装层拒绝（单引号注入）归一为 ToolError，保持调用方单一接法。"""
    async with KingdeeClient(sandbox_settings(_BASE_URL)) as client:
        tool = build_default_registry().get("erp_query_customer")
        with pytest.raises(ToolError, match="过滤条件被拒绝"):
            await tool.execute(client, {"name": "o'reilly"})


def test_default_registry_has_twenty_two_tools() -> None:
    """默认注册表含 6 个查询 + 15 个写工具 + 1 个本地知识工具。"""
    specs = build_default_registry().all_specs()
    assert {spec.name for spec in specs} == EXPECTED_TOOL_NAMES
    assert len(specs) == 22


def test_get_unknown_tool_carries_known_list() -> None:
    """未知工具名抛 ToolError，context 带已注册名单（LLM 幻觉纠错线索）。"""
    with pytest.raises(ToolError, match="未知工具") as exc_info:
        build_default_registry().get("erp_query_ufo")
    assert "erp_query_sales_order" in exc_info.value.context["known"]


def test_register_duplicate_name_rejected() -> None:
    """同名重复注册直接拒绝（装配期错误尽早暴露）。"""
    registry = build_default_registry()
    duplicate = ToolDefinition(
        spec=ToolSpec(name="erp_query_sales_order", description="dup", parameters={}),
        params_model=SalesOrderParams,
        build_filter=lambda _params: FilterExpression(),
        endpoint=SAL_ORDER_TOOL.endpoint,
    )
    with pytest.raises(ToolError, match="重复注册"):
        registry.register(duplicate)


def test_spec_parameters_is_single_source() -> None:
    """ToolSpec.parameters 直接来自参数模型 JSON Schema（schema 即文档，零漂移）。"""
    tool = build_default_registry().get("erp_query_customer")
    assert tool.spec.parameters == CustomerParams.model_json_schema()
    assert tool.spec.parameters["properties"]["limit"]["type"] == "integer"


def test_name_property_mirrors_spec() -> None:
    """name 属性与 spec.name 一致（tool_calls 对齐键的单一来源）。"""
    tool = build_default_registry().get("erp_query_sales_order")
    assert tool.name == tool.spec.name == "erp_query_sales_order"
    assert tool.params_model is SalesOrderParams
    assert CUSTOMER_TOOL.tool_name == "erp_query_customer"
