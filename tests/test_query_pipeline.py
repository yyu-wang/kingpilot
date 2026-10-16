"""M2-T04：查询主链路单测（FakeProvider 脚本化 + respx 零网络）。

分层：本文件验「管道逻辑」（分流/回填/错误不中断/trace 完整性）；
工具内部见 test_agent_tools.py；真 LLM 端到端见 test_integration_query.py。
"""

from typing import Any

import httpx
import pytest
import respx

from agent.query_pipeline import QueryPipeline
from agent.tools import build_default_registry
from infrastructure.exceptions import LLMUnavailableError
from kingdee.client import KingdeeClient
from models.llm import PlanResult, ToolCallDecision
from models.user import UserContext
from sandbox import sandbox_settings
from sandbox.kingdee_stub.app import LOGIN_PATH, QUERY_PATH
from tests.fakes import FakeProvider

_BASE_URL = "http://kd.test"
_LOGIN_URL = f"{_BASE_URL}{LOGIN_PATH}"
_QUERY_URL = f"{_BASE_URL}{QUERY_PATH}"
# 列序 = SAL_ORDER 默认字段集；值锚定 dataset 具名样例 SO000001
_SALES_ROW = ["SO000001", "2026-01-05", "测试客户001", "C", "150000.0000000000"]
# 列序 = INVENTORY 默认字段集（4 列）
_INVENTORY_ROW = ["MAT001", "测试物料001", "成品仓", "100.0000000000"]


def _plan(*calls: tuple[str, dict[str, Any]]) -> PlanResult:
    """构造 plan 剧本元素。"""
    return PlanResult(
        tool_calls=[ToolCallDecision(tool_name=name, arguments=args) for name, args in calls]
    )


def _mock_kingdee(rows: list[list[Any]]) -> None:
    """打桩登录 + 查询（单次查询用）。"""
    respx.post(_LOGIN_URL).respond(json={"LoginResultType": 1})
    respx.post(_QUERY_URL).respond(json=rows)


async def _run(
    plan_result: PlanResult,
    response_text: str,
    user_input: str = "帮我查订单",
    user_context: UserContext | None = None,
) -> Any:
    """跑一次管道（FakeProvider 剧本 + 沙箱配置 client，respx 拦截 HTTP）。"""
    provider = FakeProvider(plan_queue=[plan_result], response_queue=[response_text])
    client = KingdeeClient(sandbox_settings(_BASE_URL))
    try:
        pipeline = QueryPipeline(provider, build_default_registry(), client)
        return await pipeline.run(user_input, user_context)
    finally:
        await client.aclose()


@respx.mock
async def test_chat_only_path_skips_tools() -> None:
    """plan 无工具调用 → 直接生成答复，零网络请求（respx 无路由，触网即红）。"""
    response = await _run(PlanResult(), "您好，我是金蝶助手", "你好")
    assert response.content == "您好，我是金蝶助手"
    assert response.trace.tool_executions == []


@respx.mock
async def test_tool_call_round_trip_fills_trace() -> None:
    """单工具全链路：执行成功 → result 回填 → 答复产出 → trace 完整。"""
    _mock_kingdee([_SALES_ROW])

    response = await _run(
        _plan(("kingdee_query_sales_order", {"limit": 2, "document_status": "C"})),
        "已为您查到 1 笔销售订单",
    )

    execution = response.trace.tool_executions[0]
    assert execution.tool_name == "kingdee_query_sales_order"
    assert execution.error is None
    assert execution.result["rows"][0]["FBillNo"] == "SO000001"  # 金额字符串原样在 result 内
    assert response.content == "已为您查到 1 笔销售订单"
    assert response.trace.final_response == response.content
    assert response.trace_id == response.trace.trace_id


@respx.mock
async def test_unknown_tool_recorded_not_raised() -> None:
    """LLM 幻觉工具名：记入 error 继续走完链路，不 crash（D2）。"""
    response = await _run(_plan(("kingdee_query_ufo", {})), "该工具暂不可用")
    assert "未知工具" in response.trace.tool_executions[0].error
    assert response.content == "该工具暂不可用"  # 答复仍生成（LLM 拿错误上下文解释）


@respx.mock
async def test_invalid_arguments_recorded_not_raised() -> None:
    """坏参数在工具层 fail-closed，错误回填不中断。"""
    response = await _run(_plan(("kingdee_query_sales_order", {"bogus": 1})), "参数有误")
    assert "参数校验失败" in response.trace.tool_executions[0].error


@respx.mock
async def test_multiple_tool_calls_executed_in_order() -> None:
    """多 tool_calls 按序执行：登录一次（会话缓存），查询各按列序回包。"""
    respx.post(_LOGIN_URL).respond(json={"LoginResultType": 1})
    respx.post(_QUERY_URL).mock(
        side_effect=[
            httpx.Response(200, json=[_SALES_ROW]),
            httpx.Response(200, json=[_INVENTORY_ROW]),
        ]
    )

    response = await _run(
        _plan(
            ("kingdee_query_sales_order", {"limit": 1}),
            ("kingdee_query_inventory", {"material_number": "MAT001"}),
        ),
        "已为您查询订单与库存",
    )

    executions = response.trace.tool_executions
    assert [e.tool_name for e in executions] == [
        "kingdee_query_sales_order",
        "kingdee_query_inventory",
    ]
    assert all(e.error is None for e in executions)
    assert executions[0].result["form_id"] == "SAL_Order"
    assert executions[1].result["form_id"] == "STK_Inventory"


@respx.mock
async def test_llm_unavailable_propagates() -> None:
    """LLM 不可用直接传播（D2：降级是 M3 主循环职责，管道不吞）。"""
    provider = FakeProvider(plan_queue=[LLMUnavailableError("连接超时")])
    client = KingdeeClient(sandbox_settings(_BASE_URL))
    try:
        pipeline = QueryPipeline(provider, build_default_registry(), client)
        with pytest.raises(LLMUnavailableError, match="连接超时"):
            await pipeline.run("查订单")
    finally:
        await client.aclose()


@respx.mock
async def test_trace_captures_planning_and_latency() -> None:
    """trace 审计字段：llm_planning 记录决策，耗时非负，默认用户 local_dev/viewer。"""
    _mock_kingdee([_SALES_ROW])

    response = await _run(_plan(("kingdee_query_sales_order", {"limit": 1})), "好的")

    assert len(response.trace.llm_planning["tool_calls"]) == 1
    assert response.trace.total_latency_ms >= 0
    assert response.trace.user_context.user_id == "local_dev"
    assert response.trace.user_context.role == "viewer"


@respx.mock
async def test_custom_user_context_flows_into_trace() -> None:
    """显式传入的 UserContext 进入 trace（M3 主循环的挂载点预演）。"""
    _mock_kingdee([_SALES_ROW])

    response = await _run(
        _plan(("kingdee_query_sales_order", {"limit": 1})),
        "好的",
        user_context=UserContext(user_id="u001", role="admin"),
    )

    assert response.trace.user_context.user_id == "u001"
    assert response.trace.user_context.role == "admin"
