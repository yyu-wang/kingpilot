"""模型注册中心雏形（M2-T02）。

Why 放 config/：全局规范 3.6「规则表与逻辑分离」——模型路由属于配置规则，
增改模型只改这张表，不改 ``agent/providers.py`` 一行代码。

雏形边界（YAGNI）：T02 只有 OpenAI 兼容协议一族 + 单路由表；
failover 链、按任务路由（plan 用 A / generate 用 B）留待 M7 成本治理。
密钥不入本表（红线：SecretStr 只进 Settings），本表只描述「路由到哪」。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRoute:
    """一条模型路由：逻辑名 → 真实服务端点。

    ``name`` 是业务侧引用的逻辑名（Settings.llm_route 指向它），
    ``model_id`` 是厂商侧真实模型标识——两层解耦后，厂商换版本
    （deepseek-chat → 后继版本）只改 ``model_id``，调用方无感。
    ``provider_kind`` 为协议族闭集，T02 仅 "openai_compatible"
    （扩族时改 Literal 收紧）。
    """

    name: str
    provider_kind: str
    base_url: str  # 如 https://api.deepseek.com（不含 /chat/completions 尾巴）
    model_id: str
    timeout_seconds: float = 30.0  # LLM 生成比普通 API 慢，默认放宽到 30s


# 单一真源：业务代码一律经逻辑名查表，禁止硬编码 model_id
MODEL_REGISTRY: dict[str, ModelRoute] = {
    "chat_default": ModelRoute(
        name="chat_default",
        provider_kind="openai_compatible",
        base_url="https://api.deepseek.com",
        # deepseek-chat 支持原生 function calling（deepseek-reasoner 不支持，勿用于 plan）
        model_id="deepseek-chat",
    ),
    # 本地开发路由（M2-T02 追加）：Ollama 的 OpenAI 兼容端点，省 key/零网络依赖。
    # 2026-10-16 实测（curl 带 tools 请求）：qwen2.5:7b 正确返回 tool_calls
    # （finish_reason=tool_calls，arguments 合法 JSON）。本机另装 qwen3.8-27b
    # 亦支持 tools，但其 message 携带非标准 `reasoning` 字段（思考模型特征），
    # 对接 reflect/generate 前需验证 content 与 reasoning 的分离行为，暂作备选。
    "ollama_qwen": ModelRoute(
        name="ollama_qwen",
        provider_kind="openai_compatible",
        base_url="http://127.0.0.1:11434/v1",
        model_id="qwen2.5:7b",
        timeout_seconds=120.0,  # 本地推理慢，放宽超时
    ),
}
