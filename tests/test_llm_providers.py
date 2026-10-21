"""M2-T02：OpenAICompatibleProvider 单测（respx 零网络）。

覆盖三类断言：
1. 协议映射——三方法到 chat/completions 报文的序列化与反序列化
2. 异常映射——四分类逐条验证（含「异常消息不含密钥」红线）
3. 契约复用——真 Provider 跑 T01 契约套件（替身与真实现同一把尺）
"""

import json
import ssl
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from agent.providers import OpenAICompatibleProvider, get_provider
from config.settings import Settings
from infrastructure.exceptions import (
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMUnavailableError,
)
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    PlanResult,
    ToolSpec,
)
from tests.test_llm_provider_contract import run_provider_contract

_URL = "https://api.deepseek.com/chat/completions"
_API_KEY = "test-key-not-a-real-credential"  # pragma: allowlist secret


def _message(**extra: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **extra}


def _body(message: dict[str, Any], *, prompt: int = 10, completion: int = 5) -> dict[str, Any]:
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def _tool_call(name: str, arguments: str) -> dict[str, Any]:
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}


def _order_tool() -> ToolSpec:
    return ToolSpec(
        name="kingdee_query_sales_order",
        description="按条件查询销售订单",
        parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
    )


def _context() -> LLMContext:
    return LLMContext(messages=[LLMMessage(role="user", content="查最近 5 笔销售订单")])


def _provider(**kwargs: Any) -> OpenAICompatibleProvider:
    # _env_file=None：禁读本地 .env（pydantic-settings 构造级覆盖）。
    # Why：env_file 机制独立于 os.environ，conftest 的环境变量隔离挡不住它——
    # 本机 .env 若切了路由（如 ollama_qwen），不显式禁用就会把单测请求
    # 打去别的 base_url。测试必须只测字段默认值，不吃宿主机配置。
    settings = Settings(_env_file=None, llm_api_key=SecretStr(_API_KEY))
    return OpenAICompatibleProvider(settings, **kwargs)


# ── 协议映射 ──


@respx.mock
async def test_plan_parses_tool_calls_and_sends_auth_header() -> None:
    route = respx.post(_URL).mock(
        return_value=httpx.Response(
            200,
            json=_body(
                _message(tool_calls=[_tool_call("kingdee_query_sales_order", '{"limit": 5}')])
            ),
        )
    )
    async with _provider() as provider:
        result = await provider.plan(_context(), [_order_tool()])
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {_API_KEY}"
    assert isinstance(result, PlanResult)
    assert result.tool_calls[0].tool_name == "kingdee_query_sales_order"
    assert result.tool_calls[0].arguments == {"limit": 5}


@respx.mock
async def test_plan_without_tool_calls_returns_empty_plan() -> None:
    """模型不调工具（纯聊天）→ PlanResult.tool_calls 为空（协议语义）。"""
    respx.post(_URL).mock(return_value=httpx.Response(200, json=_body(_message(content="你好"))))
    async with _provider() as provider:
        result = await provider.plan(_context(), [_order_tool()])
    assert result.tool_calls == []


@respx.mock
async def test_plan_serializes_tool_specs_to_openai_schema() -> None:
    """ToolSpec → OpenAI function 声明的报文形状（T04 prompt 组装的下游契约）。"""
    route = respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(_message(content="ok"))),
    )
    async with _provider() as provider:
        await provider.plan(_context(), [_order_tool()])
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "deepseek-chat"
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][0]["function"]["name"] == "kingdee_query_sales_order"


@respx.mock
async def test_plan_with_invalid_arguments_json_raises_response_error() -> None:
    """协议里 arguments 是 JSON 字符串——坏 JSON 在解析层 fail-closed，不进 Guard。"""
    respx.post(_URL).mock(
        return_value=httpx.Response(
            200, json=_body(_message(tool_calls=[_tool_call("t", "{bad json")]))
        ),
    )
    async with _provider() as provider:
        with pytest.raises(LLMResponseError):
            await provider.plan(_context(), [_order_tool()])


@respx.mock
async def test_plan_with_missing_tool_name_raises_response_error() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(_message(tool_calls=[_tool_call("", "{}")]))),
    )
    async with _provider() as provider:
        with pytest.raises(LLMResponseError):
            await provider.plan(_context(), [_order_tool()])


@respx.mock
async def test_reflect_parses_contract_json() -> None:
    reflect_json = json.dumps({"continue_execution": False, "reasoning": "结果已足够"})
    respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(_message(content=reflect_json)))
    )
    async with _provider() as provider:
        result = await provider.reflect(_context(), execution=_summary(), memory=_memory())
    assert result.continue_execution is False
    assert result.reasoning == "结果已足够"


@respx.mock
async def test_reflect_with_non_json_output_raises_response_error() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(_message(content="我觉得可以继续")))
    )
    async with _provider() as provider:
        with pytest.raises(LLMResponseError, match="reflect"):
            await provider.reflect(_context(), execution=_summary(), memory=_memory())


@respx.mock
async def test_generate_response_returns_content() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(_message(content="已为您查询到 5 笔订单"))),
    )
    async with _provider() as provider:
        text = await provider.generate_response(_context(), execution=_summary(), memory=_memory())
    assert text == "已为您查询到 5 笔订单"


@respx.mock
async def test_generate_with_empty_content_raises_response_error() -> None:
    respx.post(_URL).mock(return_value=httpx.Response(200, json=_body(_message(content=""))))
    async with _provider() as provider:
        with pytest.raises(LLMResponseError, match="空文本"):
            await provider.generate_response(_context(), execution=_summary(), memory=_memory())


# ── 用量通道与异常映射 ──


@respx.mock
async def test_usage_reported_via_sink() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(
            200, json=_body(_message(content="ok"), prompt=120, completion=30)
        ),
    )
    reported: list[Any] = []
    async with _provider(usage_sink=reported.append) as provider:
        await provider.generate_response(_context(), execution=_summary(), memory=_memory())
    assert len(reported) == 1
    assert reported[0].prompt_tokens == 120
    assert reported[0].total_tokens == 150


@respx.mock
async def test_401_raises_llm_error_without_leaking_key() -> None:
    respx.post(_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "auth failed"}})
    )
    async with _provider() as provider:
        with pytest.raises(LLMError) as exc_info:
            await provider.plan(_context(), [_order_tool()])
    assert _API_KEY not in str(exc_info.value)


@respx.mock
async def test_429_raises_rate_limit_error() -> None:
    respx.post(_URL).mock(return_value=httpx.Response(429, json={}))
    async with _provider() as provider:
        with pytest.raises(LLMRateLimitError):
            await provider.plan(_context(), [_order_tool()])


@respx.mock
async def test_5xx_raises_unavailable_error() -> None:
    """5xx 属「暂时不可用」——主循环降级触发点。"""
    respx.post(_URL).mock(return_value=httpx.Response(503, json={}))
    async with _provider() as provider:
        with pytest.raises(LLMUnavailableError):
            await provider.plan(_context(), [_order_tool()])


@respx.mock
async def test_timeout_raises_unavailable_error() -> None:
    respx.post(_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    async with _provider() as provider:
        with pytest.raises(LLMUnavailableError):
            await provider.plan(_context(), [_order_tool()])


@respx.mock
async def test_ssl_error_is_retried_once_then_succeeds() -> None:
    """陈旧 keep-alive 连接偶发 SSLError：单次重试换新连接后应正常返回。"""
    route = respx.post(_URL)
    route.side_effect = [
        ssl.SSLError(1, "bad record mac"),
        httpx.Response(200, json=_body(_message(content="ok"))),
    ]
    async with _provider() as provider:
        text = await provider.generate_response(_context(), execution=_summary(), memory=_memory())
    assert text == "ok"
    assert route.call_count == 2


@respx.mock
async def test_persistent_ssl_error_maps_to_unavailable() -> None:
    """重试仍失败 → LLMUnavailableError（主循环降级），不冒泡为「服务内部错误」。"""
    route = respx.post(_URL)
    route.side_effect = [ssl.SSLError(1, "bad record mac"), ssl.SSLError(1, "bad record mac")]
    async with _provider() as provider:
        with pytest.raises(LLMUnavailableError):
            await provider.plan(_context(), [_order_tool()])
    assert route.call_count == 2


@respx.mock
async def test_malformed_body_raises_response_error() -> None:
    """HTTP 200 但结构缺 choices/usage —— fail-closed 而非 KeyError 冒泡。"""
    respx.post(_URL).mock(return_value=httpx.Response(200, json={"nope": True}))
    async with _provider() as provider:
        with pytest.raises(LLMResponseError):
            await provider.plan(_context(), [_order_tool()])


# ── 工厂与构造期校验 ──


def test_get_provider_with_unknown_route_fails_fast() -> None:
    settings = Settings(_env_file=None, llm_route="no_such_route", llm_api_key=SecretStr(_API_KEY))
    with pytest.raises(LLMError, match="未知 LLM 路由"):
        get_provider(settings)


def test_provider_without_api_key_fails_at_construction() -> None:
    """密钥缺失在构造期报错（配置错误尽早暴露），不等第一次调用。"""
    with pytest.raises(LLMError, match="密钥未配置"):
        # _env_file=None：测的是字段默认值（空 SecretStr），不吃本机 .env 的 key
        OpenAICompatibleProvider(Settings(_env_file=None))


# ── 契约复用：真 Provider 跑 T01 契约套件 ──


@respx.mock
async def test_real_provider_passes_contract_suite() -> None:
    """T01 契约测试的直接回报：真实现与替身被同一套断言度量。"""
    route = respx.post(_URL)
    summary_json = json.dumps(
        {
            "summary_text": "用户查询了客户 CUS001 的订单",
            "key_entities": [
                {"entity_type": "customer", "display_name": "客户 CUS001", "kingdee_id": "CUS001"}
            ],
            "unresolved_questions": [],
            "tool_results_cache": {"order": "SO000008"},
        }
    )
    route.side_effect = [
        # 1) plan → tool_calls；2) reflect → JSON；3) generate → 文本；4) summarize → JSON
        httpx.Response(
            200,
            json=_body(
                _message(tool_calls=[_tool_call("kingdee_query_sales_order", '{"limit": 5}')])
            ),
        ),
        httpx.Response(
            200, json=_body(_message(content=json.dumps({"continue_execution": False})))
        ),
        httpx.Response(200, json=_body(_message(content="已为您查询到订单"))),
        httpx.Response(200, json=_body(_message(content=summary_json))),
    ]
    async with _provider() as provider:
        await run_provider_contract(provider)


@respx.mock
async def test_summarize_with_non_json_output_raises_response_error() -> None:
    """summarize 输出非约定 JSON → LLMResponseError（压缩器据此放行不阻断）。"""
    respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(_message(content="我没法压缩成 JSON")))
    )
    async with _provider() as provider:
        with pytest.raises(LLMResponseError, match="summarize"):
            await provider.summarize([LLMMessage(role="user", content="旧对话")])


# ── 测试辅助 ──


def _summary() -> ExecutionSummary:
    return ExecutionSummary()


def _memory() -> MemorySnapshot:
    return MemorySnapshot()
