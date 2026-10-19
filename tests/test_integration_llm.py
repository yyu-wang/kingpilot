"""M2-T02：真 LLM 集成验收（integration 标记，默认门禁不跑）。

跑法：``make test-integration``。未配置 ``KINGPILOT_LLM_API_KEY`` 时
自动 skip（不报错）——CI 无密钥环境与本任务验收互不阻塞。

Why 用真实调用而非 mock：本文件验收的就是「真实报文往返」——
DeepSeek 对 function calling 的实际响应形状、usage 字段、鉴权头，
respx 模拟不了厂商行为漂移（模型升级/协议调整）。
"""

import pytest

from agent.providers import OpenAICompatibleProvider, get_provider
from config.settings import Settings
from governance.cost_engine import RoutingProvider
from models.llm import LLMContext, LLMMessage, PlanResult, ToolSpec

_ORDER_TOOL = ToolSpec(
    name="kingdee_query_sales_order",
    description=(
        "按条件查询销售订单列表。参数 limit 为返回条数上限（整数，1-100）。"
        "仅当用户明确要查销售订单时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {"limit": {"type": "integer", "description": "返回条数上限"}},
    },
)


def _real_provider() -> OpenAICompatibleProvider | RoutingProvider:
    """返回真 Provider；未配 key 则 skip（Settings 从 .env 读真凭证）。"""
    settings = Settings()
    if not settings.llm_api_key.get_secret_value():
        pytest.skip("未配置 KINGPILOT_LLM_API_KEY，跳过真 LLM 集成测试")
    return get_provider(settings)


@pytest.mark.integration
async def test_real_llm_plan_selects_tool() -> None:
    """真调用 plan：模型应从工具声明中选中查询工具并给出合法参数。

    打哪个模型由 ``.env`` 的 ``KINGPILOT_LLM_ROUTE`` 决定
    （deepseek-chat / 本地 Ollama 千问均可）。
    """
    provider = _real_provider()
    context = LLMContext(messages=[LLMMessage(role="user", content="帮我查一下最近 5 笔销售订单")])
    try:
        result = await provider.plan(context, [_ORDER_TOOL])
    finally:
        await provider.aclose()
    assert isinstance(result, PlanResult)
    assert result.tool_calls, "模型未选择任何工具"
    assert result.tool_calls[0].tool_name == "kingdee_query_sales_order"


@pytest.mark.integration
async def test_real_llm_plan_without_tool_intent_returns_empty() -> None:
    """纯聊天意图（无查询需求）→ 空工具调用（协议「空 = 纯聊天」语义）。"""
    provider = _real_provider()
    context = LLMContext(messages=[LLMMessage(role="user", content="你好，你是谁？")])
    try:
        result = await provider.plan(context, [_ORDER_TOOL])
    finally:
        await provider.aclose()
    assert isinstance(result, PlanResult)
    assert result.tool_calls == []
