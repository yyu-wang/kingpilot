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

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

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
    SummaryDraft,
    TokenUsage,
    ToolCallDecision,
    ToolSpec,
)

if TYPE_CHECKING:
    from governance.cost_engine import RoutingProvider
    from observability.audit import AuditLogger

_CHAT_COMPLETIONS_PATH = "/chat/completions"
# 自动路由哨兵：按 Settings.llm_tier 走 ROUTING_CONFIG（M7-T03）
_AUTO_ROUTES = frozenset({"", "chat_default"})

# 记忆压缩指令（M3-T03，方案 §5.3 压缩 Prompt 要点：严格 JSON + 只留有业务意义的内容）
_SUMMARIZE_INSTRUCTION = (
    "你是对话压缩器。把给定的旧对话压缩为严格 JSON（不要输出 JSON 之外的任何内容）：\n"
    '{"summary_text": "1-3 句任务脉络，必须保留客户/订单等指代对象的名称与编码", '
    '"key_entities": [{"entity_type": "customer|order|supplier|material|other", '
    '"display_name": "名称", "kingdee_id": "对话中出现的编码，没有则空串"}], '
    '"unresolved_questions": ["尚未解决、后续可能追问的问题"], '
    '"tool_results_cache": {"业务键": "有业务意义的数字或名称"}}\n'
    "要求：不编造对话中不存在的实体与数字；tool_results_cache 只留有业务意义的"
    "数字和名称，不要放大段原始报文。"
)


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

    async def __aenter__(self) -> OpenAICompatibleProvider:
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
        # M3-T05 实测强化：只给「回 JSON」时模型会输出名称片段当续跑指令并重复查询；
        # 且易把「本步成功」误判为「任务完成」。判定口径必须是「用户全部请求」。
        instruction = (
            "你是企业 ERP 查询 Agent 的反思层。判断用户**全部请求**是否已经完成"
            "（不是判断当前这一步是否成功）。只回 JSON："
            '{"continue_execution": true/false, "next_input": "...", "reasoning": "..."}\n'
            "规则：\n"
            "1) 用户请求已全部完成、或无需工具即可回答 → continue_execution=false；\n"
            "2) 还有未完成动作 → continue_execution=true。例如用户要求「先查客户编码，"
            "再查其订单」，目前只查到编码 → 必须 true，next_input 写「用客户编码 CUS001"
            "查询销售订单」这类完整指令；\n"
            "3) next_input 必须完整、可独立执行，禁止只给名称/编码等片段；\n"
            "4) 结果为空且参数可能有误（例如把客户名称当成客户编码）→ next_input 指明"
            "先用 kingdee_query_customer 按名称查询客户编码；\n"
            "5) 不要重复刚执行过的相同调用与参数。"
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

    async def summarize(self, messages: Sequence[LLMMessage]) -> SummaryDraft:
        """记忆压缩：旧对话 + 压缩指令（system）→ 严格 JSON → SummaryDraft。

        与 reflect 同款的 fail-closed 解析：坏 JSON / 字段不符归
        LLMResponseError（压缩器据此放行不阻断主对话，见 D4）。
        """
        request_messages = [
            LLMMessage(role="system", content=_SUMMARIZE_INSTRUCTION),
            *messages,
        ]
        text = await self._chat_text(LLMContext(messages=request_messages))
        try:
            return SummaryDraft.model_validate(json.loads(text))
        except ValueError as exc:
            raise LLMResponseError(
                "summarize 输出不符合约定 JSON",
                context={"raw_head": text[:200]},
            ) from exc

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
    settings: Settings,
    *,
    usage_sink: TokenUsageSink | None = None,
    audit: AuditLogger | None = None,
) -> OpenAICompatibleProvider | RoutingProvider:
    """工厂：自动路由走档位选择，显式路由固定单 Provider。

    - ``llm_route`` 为 ``chat_default`` / 空 → ``RoutingProvider``（M7-T03）
    - 其他逻辑名 → 单路由覆盖，忽略 ``llm_tier``（兼容旧配置）

    ``audit`` 供自动路由的预算降档/耗尽事件进统一审计流（M7-T04）。
    局部 import 避免 ``providers ↔ cost_engine`` 循环。
    """
    if settings.llm_route in _AUTO_ROUTES:
        from governance.cost_engine import RoutingProvider

        return RoutingProvider(settings, usage_sink=usage_sink, audit=audit)
    return OpenAICompatibleProvider(settings, usage_sink=usage_sink)
