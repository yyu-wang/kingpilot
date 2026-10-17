"""OpenAI 兼容协议 Provider（M2-T02，默认 DeepSeek）。

Why 不用厂商 SDK（决策 D2）：
- 项目统一 httpx（与金蝶 client 同栈），respx 测试设施零新增；
- SDK 封死超时/错误映射/日志注入点，企业级需要自己控制；
- 自定义 ``LLMProvider`` 协议与 SDK 类型体系天然错位。
OpenAI 兼容协议本质是 ``POST {base_url}/chat/completions`` 的 JSON 往返，
DeepSeek/GLM/Qwen 均兼容此形状。

异常映射（对齐 T01 四分类，降级语义由主循环消费）：
- 超时/网络/5xx → ``LLMUnavailableError``（唯一降级触发点）
- 429 → ``LLMRateLimitError``
- 401/400 等确定性错误 → ``LLMError``（不降级——配置错误重试无意义）
- 响应非 JSON / 结构不符 / 工具参数非 JSON → ``LLMResponseError``（fail-closed）

安全红线：API key 只进 ``Authorization`` 头，永不出现在异常消息与日志中。
"""

import json
from collections.abc import Sequence
from typing import Any

import httpx

from agent.llm import TokenUsageSink
from config.model_registry import MODEL_REGISTRY, ModelRoute
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
    ReflectionResult,
    TokenUsage,
    ToolCallDecision,
    ToolSpec,
)

_CHAT_COMPLETIONS_PATH = "/chat/completions"


class OpenAICompatibleProvider:
    """``LLMProvider`` 协议的 OpenAI 兼容实现（无继承，结构化满足协议）。

    ``http`` 注入供 respx 单测；生产经 ``get_provider`` 工厂自建连接池，
    ``aclose()`` 由调用方（M3 主循环）在会话结束时显式调用。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        route: ModelRoute | None = None,
        http: httpx.AsyncClient | None = None,
        usage_sink: TokenUsageSink | None = None,
    ) -> None:
        self._route = route or self._resolve_route(settings)
        self._usage_sink = usage_sink
        # fail-fast：密钥缺失在构造期报错，不等第一次调用才发现（配置错误应尽早暴露）
        self._api_key = settings.llm_api_key.get_secret_value()
        if not self._api_key:
            raise LLMError(
                "LLM API 密钥未配置",
                context={"route": self._route.name, "hint": "KINGPILOT_LLM_API_KEY"},
            )
        self._temperature = settings.llm_temperature
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(self._route.timeout_seconds))

    @staticmethod
    def _resolve_route(settings: Settings) -> ModelRoute:
        """按逻辑名查路由表；未知逻辑名 fail-fast（配置拼写错误当场暴露）。"""
        route = MODEL_REGISTRY.get(settings.llm_route)
        if route is None:
            known = ", ".join(sorted(MODEL_REGISTRY))
            raise LLMError(
                f"未知 LLM 路由: {settings.llm_route}",
                context={"known_routes": known},
            )
        return route

    async def aclose(self) -> None:
        """关闭自建连接池（注入 http 时不动调用方的资源）。"""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "OpenAICompatibleProvider":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ── LLMProvider 协议实现 ──

    async def plan(self, context: LLMContext, tools: Sequence[ToolSpec]) -> PlanResult:
        """Phase 1 规划：带 tools 调用，解析 function calling 决策。"""
        message = await self._chat_once(context, tools=tools)
        return PlanResult(tool_calls=self._parse_tool_calls(message))

    async def reflect(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> ReflectionResult:
        """Phase 4 反思：摘要序列化为 user 消息，约定模型回固定 JSON。"""
        snapshot = {
            "tool_executions": [te.model_dump() for te in execution.tool_executions],
            "needs_more_steps": execution.needs_more_steps,
            "memory": memory.model_dump(),
        }
        instruction = (
            "根据执行摘要判断是否足以回答用户。只回 JSON："
            '{"continue_execution": true/false, "next_input": "...", "reasoning": "..."}'
        )
        messages = [
            *context.messages,
            LLMMessage(
                role="user", content=f"{instruction}\n{json.dumps(snapshot, ensure_ascii=False)}"
            ),
        ]
        text = await self._chat_text(LLMContext(messages=messages, anchors=context.anchors))
        try:
            return ReflectionResult.model_validate(json.loads(text))
        except ValueError as exc:
            # ValueError 覆盖 JSON 解析失败与 Pydantic 校验失败——归为响应不合法
            raise LLMResponseError(
                "reflect 输出不符合约定 JSON",
                context={"raw_head": text[:200]},
            ) from exc

    async def generate_response(
        self, context: LLMContext, execution: ExecutionSummary, memory: MemorySnapshot
    ) -> str:
        """Phase 4 出稿：执行摘要随上下文送入，取纯文本答复。"""
        snapshot = {
            "tool_executions": [te.model_dump() for te in execution.tool_executions],
            "memory": memory.model_dump(),
        }
        instruction = "基于执行摘要生成面向用户的答复，禁止编造摘要之外的数字或单据号。"
        messages = [
            *context.messages,
            LLMMessage(
                role="user", content=f"{instruction}\n{json.dumps(snapshot, ensure_ascii=False)}"
            ),
        ]
        return await self._chat_text(LLMContext(messages=messages, anchors=context.anchors))

    # ── 内部：请求发送与响应解析 ──

    async def _chat_once(self, context: LLMContext, *, tools: Sequence[ToolSpec]) -> dict[str, Any]:
        """发一次 chat/completions，返回 choices[0].message 原始对象。"""
        payload: dict[str, Any] = {
            "model": self._route.model_id,
            "messages": self._serialize_messages(context),
            # 温度随请求透传（默认 0.0 贪心解码）：查询与评测都要确定性输出
            "temperature": self._temperature,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        url = self._route.base_url.rstrip("/") + _CHAT_COMPLETIONS_PATH
        try:
            resp = await self._http.post(
                url, json=payload, headers={"Authorization": f"Bearer {self._api_key}"}
            )
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError("LLM 请求超时", context={"route": self._route.name}) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"LLM 网络错误: {type(exc).__name__}", context={"route": self._route.name}
            ) from exc
        return self._extract_message(resp)

    async def _chat_text(self, context: LLMContext) -> str:
        """无工具 chat，返回非空 content（空响应 fail-closed）。"""
        message = await self._chat_once(context, tools=[])
        content = message.get("content")
        if not content or not str(content).strip():
            raise LLMResponseError("LLM 返回空文本", context={"route": self._route.name})
        return str(content)

    @staticmethod
    def _serialize_messages(context: LLMContext) -> list[dict[str, Any]]:
        """项目消息 → OpenAI 协议消息（role/content 必带，关联字段非空才带）。"""
        out: list[dict[str, Any]] = []
        for m in context.messages:
            item: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.name:
                item["name"] = m.name
            if m.tool_call_id:
                item["tool_call_id"] = m.tool_call_id
            out.append(item)
        return out

    def _extract_message(self, resp: httpx.Response) -> dict[str, Any]:
        """状态码分流 + JSON/结构校验 + usage 上报，返回 message 字典。"""
        if resp.status_code == 429:
            raise LLMRateLimitError("LLM 供应商限流", context={"status": 429})
        if resp.status_code >= 500:
            raise LLMUnavailableError(
                f"LLM 服务端错误: HTTP {resp.status_code}", context={"route": self._route.name}
            )
        if resp.status_code >= 400:
            # 401/400 等确定性错误：消息只带状态码，绝不带 Authorization 头内容
            raise LLMError(
                f"LLM 请求被拒: HTTP {resp.status_code}", context={"status": resp.status_code}
            )
        try:
            body = resp.json()
            raw_message = body["choices"][0]["message"]
            usage = body["usage"]
            prompt, completion = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(
                f"LLM 响应结构不符: {type(exc).__name__}", context={"route": self._route.name}
            ) from exc
        if not isinstance(raw_message, dict):
            raise LLMResponseError(
                f"message 应为对象，实际 {type(raw_message).__name__}",
                context={"route": self._route.name},
            )
        if self._usage_sink is not None:
            self._usage_sink(TokenUsage(prompt_tokens=prompt, completion_tokens=completion))
        return raw_message

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCallDecision]:
        """OpenAI ``tool_calls`` → 项目决策对象。

        Why 逐项 fail-closed：``arguments`` 在协议里是 **JSON 字符串**而非对象，
        解析失败/工具名为空都当场报 ``LLMResponseError``——带病参数进 Guard
        之前就被拦下，错误定位在协议解析层而非护栏层。
        """
        decisions: list[ToolCallDecision] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            if not name:
                raise LLMResponseError("tool_call 缺少工具名", context={"raw": str(fn)[:200]})
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except ValueError as exc:
                raise LLMResponseError(
                    f"工具参数不是合法 JSON: {name}",
                    context={"raw_head": str(fn.get("arguments"))[:200]},
                ) from exc
            decisions.append(ToolCallDecision(tool_name=name, arguments=arguments))
        return decisions


def get_provider(
    settings: Settings, *, usage_sink: TokenUsageSink | None = None
) -> OpenAICompatibleProvider:
    """工厂：按 Settings 查路由表建 Provider（生产入口，测试直接构造注入）。"""
    return OpenAICompatibleProvider(settings, usage_sink=usage_sink)
