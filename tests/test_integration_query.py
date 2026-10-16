"""M2-T04：查询主链路端到端集成验收（真 LLM + 仿真沙箱，标记 integration）。

3 类典型问题（任务卡验收），全部单工具可答（D1——名称换码等双跳编排归 M3）。
LLM 路由读 .env（KINGPILOT_LLM_ROUTE）：本地 Ollama 千问 / DeepSeek 均可跑；
小模型 function calling 可靠性有限，偶发失败可切 chat_default 复跑对照。
"""

import json
from typing import Any

import pytest

from agent.providers import get_provider
from agent.query_pipeline import QueryPipeline
from agent.tools import build_default_registry
from config.settings import get_settings
from kingdee.client import KingdeeClient

pytestmark = pytest.mark.integration


async def _run_pipeline(client: KingdeeClient, question: str) -> Any:
    """真 LLM + 沙箱 client 跑一轮（provider 用完即关，不泄漏连接）。"""
    provider = get_provider(get_settings())
    try:
        pipeline = QueryPipeline(provider, build_default_registry(), client)
        return await pipeline.run(question)
    finally:
        await provider.aclose()


async def test_pipeline_recent_sales_orders(client: KingdeeClient) -> None:
    """类型①：查最近订单 → 选中销售订单工具 → 返回锚点数据。"""
    response = await _run_pipeline(client, "帮我查一下最近 5 笔销售订单")

    execution = response.trace.tool_executions[0]
    assert execution.tool_name == "kingdee_query_sales_order"
    assert execution.error is None
    assert any(row["FBillNo"] == "SO000001" for row in execution.result["rows"])
    assert response.content  # 自然语言答复非空


async def test_pipeline_customer_filtered_orders(client: KingdeeClient) -> None:
    """类型②：按客户编码查订单（编码过滤；名称换码双跳归 M3）。"""
    response = await _run_pipeline(client, "查一下客户 CUS001 的销售订单")

    execution = response.trace.tool_executions[0]
    assert execution.tool_name == "kingdee_query_sales_order"
    assert execution.error is None
    assert "CUS001" in json.dumps(execution.parameters)


async def test_pipeline_inventory_by_material(client: KingdeeClient) -> None:
    """类型③：按物料编码查库存。"""
    response = await _run_pipeline(client, "MAT001 这个物料的库存有多少")

    execution = response.trace.tool_executions[0]
    assert execution.tool_name == "kingdee_query_inventory"
    assert execution.error is None
    assert "MAT001" in json.dumps(execution.parameters)
