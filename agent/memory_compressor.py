"""记忆压缩器（M3-T03，方案 §5.3 支柱九）。

触发 → 窗口 → LLM 摘要 → 引用留存：
- ``compression_reason``：三触发条件判定（urgent/aggressive/rolling），纯函数
- ``maybe_compress``：判定 + 绝对轮次窗口换算 + 调 LLM 摘要 + 实体入图 +
  摘要挂载 + 旧原文清除，返回本次摘要（未触发/失败返回 None）

关键规则：
- 轮次引用是**会话绝对轮次**（D3）：``last_to``（既有摘要的最大 to_round）
  + 当前相对轮数换算，保证多次压缩范围不重叠
- 压缩失败放行（D4）：压缩是优化手段，LLM 报错不阻断用户主对话
- 窗口为空（如单轮巨长撑爆预算）当前无消息内截断能力，记 warning 放行，
  消息级截断归 M7/M9
"""

import json
from collections.abc import Sequence

from loguru import logger

from agent.llm import LLMProvider
from agent.memory import MemoryStore, estimate_tokens, render_raw_for_llm
from config.settings import Settings
from infrastructure.exceptions import LLMError
from models.memory import ConversationSummary, EntityRef, RawMessage

# 触发原因码（进日志；未来 trace 需要可经此字段回放）
REASON_URGENT = "urgent"
REASON_AGGRESSIVE = "aggressive"
REASON_ROLLING = "rolling"


def compression_reason(store: MemoryStore, settings: Settings) -> str | None:
    """三触发条件判定，优先级 urgent > aggressive > rolling；都不满足返回 None。"""
    total_needed = _total_needed_tokens(store)
    if total_needed > settings.memory_token_budget:
        return REASON_URGENT
    if total_needed > settings.memory_token_budget * settings.memory_warning_ratio:
        return REASON_AGGRESSIVE
    if store.raw_rounds > settings.memory_raw_keep_rounds:
        return REASON_ROLLING
    return None


def _total_needed_tokens(store: MemoryStore) -> int:
    """三层记忆送 LLM 的总需求：L1 原文 + L2 摘要文本 + L3 实体 JSON。"""
    layer2 = sum(estimate_tokens(s.summary_text) for s in store.summaries())
    entities_payload = [
        {"type": e.entity_type, "name": e.display_name, "id": e.kingdee_id}
        for e in store.entities()
    ]
    layer3 = estimate_tokens(json.dumps(entities_payload, ensure_ascii=False))
    return store.used_tokens + layer2 + layer3


async def maybe_compress(
    provider: LLMProvider, store: MemoryStore, settings: Settings
) -> ConversationSummary | None:
    """需要压缩时执行一轮压缩并写回 store；未触发/窗口空/LLM 失败返回 None。"""
    reason = compression_reason(store, settings)
    if reason is None:
        return None

    last_to = store.summaries()[-1].compressed_to_round if store.summaries() else 0
    window, window_rounds = _split_window(store.raw_messages(), settings.memory_raw_keep_rounds)
    if not window:
        # urgent/aggressive 触发但没有可压的旧轮（最近 N 轮内撑爆预算）：
        # 当前架构无消息内截断，放行并显性留痕，避免静默超限
        logger.warning(
            "记忆达到压缩线但无可压缩窗口: reason={} rounds={}", reason, store.raw_rounds
        )
        return None

    try:
        draft = await provider.summarize(render_raw_for_llm(window))
    except LLMError as exc:
        # D4：压缩失败不阻断主对话——buffer 原样保留，下轮再试
        logger.warning("记忆压缩 LLM 失败，放行不阻断: reason={} error={}", reason, exc)
        return None

    entities = tuple(
        EntityRef(
            entity_type=d.entity_type,
            display_name=d.display_name,
            kingdee_id=d.kingdee_id,
            last_mentioned_in_round=last_to + window_rounds,
        )
        for d in draft.key_entities
    )
    summary = ConversationSummary(
        summary_text=draft.summary_text,
        key_entities=entities,
        unresolved_questions=tuple(draft.unresolved_questions),
        tool_results_cache=draft.tool_results_cache,
        compressed_from_round=last_to + 1,
        compressed_to_round=last_to + window_rounds,
        original_token_count=sum(_message_tokens(m) for m in window),
        compressed_token_count=estimate_tokens(draft.summary_text)
        + sum(estimate_tokens(f"{e.entity_type}:{e.display_name}{e.kingdee_id}") for e in entities),
    )
    store.add_summary(summary)
    for entity in entities:  # 方案压缩流程：实体提取合并进 entity_graph
        store.upsert_entity(entity)
    store.evict_raw_before_round(window_rounds + 1)  # 清相对轮次 1..window_rounds
    logger.info(
        "记忆压缩完成: reason={} 绝对轮次={}->{} token {}->{}",
        reason,
        summary.compressed_from_round,
        summary.compressed_to_round,
        summary.original_token_count,
        summary.compressed_token_count,
    )
    return summary


def _split_window(raw: Sequence[RawMessage], keep_rounds: int) -> tuple[list[RawMessage], int]:
    """按方案 raw_buffer[0:-keep_rounds] 切窗口，返回（窗口消息, 窗口轮数）。

    tool/assistant 跟随消息归入其前面最近的 user 轮：保留段从第
    (window_rounds+1) 轮的 user 消息开始，不会把上一轮的 tool 结果
    错误留在保留段（指代「上一轮结果」的信息已进摘要）。
    """
    total_rounds = sum(1 for m in raw if m.role == "user")
    window_rounds = total_rounds - keep_rounds
    if window_rounds <= 0:
        return [], 0
    window: list[RawMessage] = []
    round_no = 0
    for message in raw:
        if message.role == "user":
            round_no += 1
        if round_no <= window_rounds:
            window.append(message)
    return window, window_rounds


def _message_tokens(message: RawMessage) -> int:
    """窗口单条的实际 token 估算：空文本的 tool 消息按结构化结果估算。"""
    if message.content:
        return message.token_count
    if message.structured_result:
        return estimate_tokens(json.dumps(message.structured_result, ensure_ascii=False))
    return 0
