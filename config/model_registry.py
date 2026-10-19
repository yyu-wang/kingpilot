"""模型注册中心（M2-T02 雏形，M7-T03 完善分档路由与单价）。

Why 放 config/：全局规范 3.6「规则表与逻辑分离」——模型路由属于配置规则，
增改模型只改这张表，不改 ``governance/cost_engine.py`` 一行代码。

- ``MODEL_REGISTRY``：逻辑名 → 真实端点 + 档位 + token 单价（单一真源）
- ``ROUTING_CONFIG``：档位 → 逻辑名，预算降档（T04）只切档位即可
- 密钥不入本表（红线：SecretStr 只进 Settings），本表只描述「路由到哪」
"""

from dataclasses import dataclass
from typing import Literal

ModelTier = Literal["high", "mid", "economy"]

# 档位顺序即降档方向：high → mid → economy（T04 预算熔断使用）
TIERS: tuple[ModelTier, ...] = ("high", "mid", "economy")


@dataclass(frozen=True)
class ModelRoute:
    """一条模型路由：逻辑名 → 真实服务端点。

    ``name`` 是业务侧引用的逻辑名，``model_id`` 是厂商侧真实模型标识——
    两层解耦后，厂商换版本只改 ``model_id``，调用方无感。
    ``provider_kind`` 为协议族闭集，当前仅 "openai_compatible"。

    单价为**示例值**（USD / 百万 token），上线前按供应商账单校准；
    本地模型单价为 0，成本记账仍会记录 token 用量。
    """

    name: str
    provider_kind: str
    base_url: str  # 如 https://api.deepseek.com（不含 /chat/completions 尾巴）
    model_id: str
    timeout_seconds: float = 30.0  # LLM 生成比普通 API 慢，默认放宽到 30s
    tier: ModelTier = "mid"
    input_cost_per_1m_usd: float = 0.0
    output_cost_per_1m_usd: float = 0.0


# 单一真源：业务代码一律经逻辑名查表，禁止硬编码 model_id
MODEL_REGISTRY: dict[str, ModelRoute] = {
    "chat_default": ModelRoute(
        name="chat_default",
        provider_kind="openai_compatible",
        base_url="https://api.deepseek.com",
        # deepseek-chat 支持原生 function calling（deepseek-reasoner 不支持，勿用于 plan）
        model_id="deepseek-chat",
        tier="high",
        input_cost_per_1m_usd=0.27,
        output_cost_per_1m_usd=1.10,
    ),
    # 中档预留位：当前与 high 同模型，接入更便宜的兼容模型后只改本表
    "chat_mid": ModelRoute(
        name="chat_mid",
        provider_kind="openai_compatible",
        base_url="https://api.deepseek.com",
        model_id="deepseek-chat",
        tier="mid",
        input_cost_per_1m_usd=0.27,
        output_cost_per_1m_usd=1.10,
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
        tier="economy",
        input_cost_per_1m_usd=0.0,
        output_cost_per_1m_usd=0.0,
    ),
}

# 档位 → 逻辑名：换模型/换档位只改配置，不改路由逻辑（M7-T03）
ROUTING_CONFIG: dict[ModelTier, str] = {
    "high": "chat_default",
    "mid": "chat_mid",
    "economy": "ollama_qwen",
}
