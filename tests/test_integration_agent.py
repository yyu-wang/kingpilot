"""M3-T05：多步编排端到端集成验收（真 LLM + 真实 Tool 层，标记 integration）。

任务卡验收：两步任务（先查客户 → 再查其订单）integration 通过。

为什么必须真 LLM：多步编排的「是否继续」「下一步做什么」由真实模型判断，
FakeProvider 只能验证编排骨架；本文件验收模型在观察回灌后能否自主完成
「客户名称 → 编码 → 订单」两步。

Why 金蝶端点用桩：仿真沙箱（sandbox/）刻意只实现 BD 之外已验证的
SAL_Order/STK_Inventory，BD_Customer 字段全部 INFERRED 未真环境复核
（见 dataset.py 注释）——造客户数据等于把未验证字段名固化进测试设施。
故此处保留**真实 ToolDefinition/ToolRegistry/协议层**，仅将两个查询端点
的结果换成确定性桩；T01 真凭证补齐后可平移到沙箱真端点。

未配 LLM 密钥时自动 skip（CI 环境与本次验收互不阻塞）。
"""

import json
from typing import Any

import pytest
from pydantic import Field

from agent.core import AgentCore
from agent.memory import MemoryStore
from agent.providers import get_provider
from agent.tools import ToolDefinition, ToolRegistry
from config.settings import get_settings
from kingdee.endpoints import SAL_ORDER
from kingdee.endpoints.base import EndpointParams
from models.kingdee import BillQueryResult
from models.llm import ToolSpec

pytestmark = pytest.mark.integration

_CUSTOMER_TOOL = "kingdee_query_customer"
_ORDER_TOOL = "kingdee_query_sales_order"


class _CustomerParams(EndpointParams):
    """客户名称模糊查询参数（与真实端点同形）。"""

    name: str = Field(default="", description="客户名称（模糊匹配，可只给部分字）")


class _OrderParams(EndpointParams):
    """订单查询参数：只支持客户编码过滤（与真实端点同形）。"""

    customer_number: str = Field(
        default="",
        description="客户编码精确匹配（如 CUS001）；只有客户名称时请先用客户查询工具换编码",
    )


class _CustomerLookupTool(ToolDefinition):
    """桩：按名称返回测试客户001 → CUS001。"""

    async def execute(self, client: Any, arguments: dict[str, Any]) -> BillQueryResult:
        return BillQueryResult(
            form_id="BD_Customer",
            field_keys=["FNumber", "FName"],
            rows=[{"FNumber": "CUS001", "FName": "测试客户001"}],
        )


class _OrderLookupTool(ToolDefinition):
    """桩：仅当客户编码为 CUS001 时返回两笔订单（验证换码是否真的用上）。"""

    async def execute(self, client: Any, arguments: dict[str, Any]) -> BillQueryResult:
        number = str(arguments.get("customer_number") or "")
        rows: list[dict[str, Any]] = (
            [
                {"FBillNo": "SO000001", "FCustomerID.FNumber": "CUS001"},
                {"FBillNo": "SO000008", "FCustomerID.FNumber": "CUS001"},
            ]
            if number == "CUS001"
            else []
        )
        return BillQueryResult(
            form_id="SAL_Order",
            field_keys=["FBillNo", "FCustomerID.FNumber"],
            rows=rows,
        )


def _stub_registry() -> ToolRegistry:
    """两个桩工具：描述与真实工具一致（模型看到的使用指引不变）。"""
    registry = ToolRegistry()
    registry.register(
        _CustomerLookupTool(
            spec=ToolSpec(
                name=_CUSTOMER_TOOL,
                description=(
                    "按名称模糊查询客户基础资料。何时用：需要把客户名称换成客户编码时"
                    "（订单查询只支持编码过滤）。返回客户编码/名称。"
                ),
                parameters=_CustomerParams.model_json_schema(),
            ),
            params_model=_CustomerParams,
            build_filter=lambda _p: "",
            endpoint=SAL_ORDER,
        )
    )
    registry.register(
        _OrderLookupTool(
            spec=ToolSpec(
                name=_ORDER_TOOL,
                description=(
                    "按条件查询销售订单列表。何时用：用户询问某客户的订单时。"
                    "客户仅支持按编码过滤，只有客户名称时请先用 kingdee_query_customer 查编码。"
                ),
                parameters=_OrderParams.model_json_schema(),
            ),
            params_model=_OrderParams,
            build_filter=lambda _p: "",
            endpoint=SAL_ORDER,
        )
    )
    return registry


async def test_two_step_customer_name_then_orders() -> None:
    """「先查测试客户001的客户编码，再用编码查销售订单」：典型两步换码任务。

    断言三件事（缺一不可）：
    1. 两个工具都真实执行成功（模型确实完成了换码与查询）；
    2. 订单查询参数带上了换码得到的 CUS001（观察回灌生效）；
    3. 实体图沉淀客户、最终答复非空。
    """
    settings = get_settings()
    if not settings.llm_api_key.get_secret_value():
        pytest.skip("未配置 KINGPILOT_LLM_API_KEY，跳过真 LLM 集成测试")

    provider = get_provider(settings)
    memory = MemoryStore(session_id="integration-two-step")
    try:
        # 桩工具覆盖 execute，不碰 client
        core = AgentCore(
            provider,
            _stub_registry(),
            client=None,  # type: ignore[arg-type]
            settings=settings,
            memory=memory,
        )
        response = await core.run("先查测试客户001的客户编码，再用这个编码查它的销售订单")
    finally:
        await provider.aclose()

    trace = response.trace
    assert trace is not None
    tool_names = [execution.tool_name for execution in trace.tool_executions]
    assert _CUSTOMER_TOOL in tool_names
    assert _ORDER_TOOL in tool_names
    assert all(execution.error is None for execution in trace.tool_executions)

    order_execution = next(e for e in trace.tool_executions if e.tool_name == _ORDER_TOOL)
    assert "CUS001" in json.dumps(order_execution.parameters, ensure_ascii=False)
    assert response.content

    # 实体图应沉淀客户与订单（T04 与多步编排协同）
    assert memory.find_entity("customer", kingdee_id="CUS001") is not None
