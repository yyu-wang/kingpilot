"""M3-T02：三层记忆读写接口单测（零依赖，纯内存状态）。

覆盖：Layer 1 追加/计量/轮数、Layer 2 摘要挂载与旧原文清除、
Layer 3 实体去重合并（内码键与名称退化键）、LLM 快照契约。
"""

from agent.memory import MemoryStore, estimate_tokens
from models.llm import MemorySnapshot
from models.memory import ConversationSummary, EntityRef, UserProfile


def _summary(text: str, from_round: int, to_round: int) -> ConversationSummary:
    """测试用摘要（只关心文本与原文范围，其余字段给固定小值）。"""
    return ConversationSummary(
        summary_text=text,
        key_entities=(),
        unresolved_questions=(),
        tool_results_cache={},
        compressed_from_round=from_round,
        compressed_to_round=to_round,
        original_token_count=100,
        compressed_token_count=20,
    )


# ── token 估算 ──


def test_estimate_tokens_cjk_and_latin() -> None:
    """CJK 单字 1 token、其余 4 字符/token 向上取整。"""
    assert estimate_tokens("你好") == 2  # 2 个 CJK
    assert estimate_tokens("abcd") == 1  # 4 拉丁字符向上取整 1
    assert estimate_tokens("你好abc") == 3  # 2 CJK + 3 拉丁 → 2 + 1
    assert estimate_tokens("") == 0


# ── Layer 1 ──


def test_add_raw_appends_with_token_count() -> None:
    """add_raw 追加原文且 token_count 自动估算。"""
    store = MemoryStore(session_id="s1")

    message = store.add_raw("user", "你好")

    assert message.role == "user"
    assert message.content == "你好"
    assert message.token_count == 2
    assert store.used_tokens == 2


def test_raw_rounds_counts_user_messages_only() -> None:
    """轮数按 user 消息计，assistant/tool 跟随消息不另计轮。"""
    store = MemoryStore(session_id="s1")
    store.add_raw("user", "第一句")
    store.add_raw("assistant", "回复一")
    store.add_raw("tool", "结果一", structured_result={"a": 1})
    store.add_raw("user", "第二句")

    assert store.raw_rounds == 2


def test_used_tokens_sums_layer1_only() -> None:
    """used_tokens 只合计 Layer 1（摘要不重复计入）。"""
    store = MemoryStore(session_id="s1")
    store.add_raw("user", "你好")  # 2
    store.add_raw("assistant", "abcd")  # 1
    store.add_summary(_summary("摘要", 1, 1))  # 不计入 used_tokens

    assert store.used_tokens == 3


# ── Layer 2 ──


def test_add_summary_and_evict_raw_before_round() -> None:
    """压缩后：摘要保留、旧轮原文按轮清除、保留轮完整不动。"""
    store = MemoryStore(session_id="s1")
    store.add_raw("user", "第一轮问题")
    store.add_raw("assistant", "第一轮回复")
    store.add_raw("user", "第二轮问题")
    store.add_raw("assistant", "第二轮回复")
    store.add_raw("user", "第三轮问题")
    store.add_raw("assistant", "第三轮回复")

    store.add_summary(_summary("前两轮的摘要", 1, 2))
    store.evict_raw_before_round(3)

    snapshot = store.to_snapshot()
    assert snapshot.session_summary == "前两轮的摘要"
    assert store.raw_rounds == 1  # 只剩第三轮
    assert "第三轮问题" in snapshot.layer1_recent[0]
    assert all("第一轮" not in line and "第二轮" not in line for line in snapshot.layer1_recent)


# ── Layer 3 ──


def test_upsert_entity_dedup_by_kingdee_id_merges() -> None:
    """同 (类型, 内码) 去重：提及轮次取大、属性合并、不新增。"""
    store = MemoryStore(session_id="s1")
    store.upsert_entity(
        EntityRef(
            entity_type="customer",
            display_name="测试客户",
            kingdee_id="CUS001",
            last_mentioned_in_round=1,
            attributes={"city": "深圳"},
        )
    )
    store.upsert_entity(
        EntityRef(
            entity_type="customer",
            display_name="测试客户",
            kingdee_id="CUS001",
            last_mentioned_in_round=3,
            attributes={"level": "VIP"},
        )
    )

    entities = store.entities()
    assert len(entities) == 1
    merged = entities[0]
    assert merged.last_mentioned_in_round == 3
    assert merged.attributes == {"city": "深圳", "level": "VIP"}


def test_upsert_entity_falls_back_to_name_without_id() -> None:
    """内码未知时按 (类型, 名称) 去重；后补内码视为新实体（合并归 T04）。"""
    store = MemoryStore(session_id="s1")
    store.upsert_entity(
        EntityRef(entity_type="customer", display_name="测试客户", last_mentioned_in_round=1)
    )
    store.upsert_entity(
        EntityRef(entity_type="customer", display_name="测试客户", last_mentioned_in_round=2)
    )
    store.upsert_entity(
        EntityRef(
            entity_type="customer",
            display_name="测试客户",
            kingdee_id="CUS001",
            last_mentioned_in_round=3,
        )
    )

    assert len(store.entities()) == 2  # 名称占位一条 + 带内码一条
    assert len(store.entities("customer")) == 2


def test_entities_filter_by_type() -> None:
    """entities(entity_type=) 按类型过滤。"""
    store = MemoryStore(session_id="s1")
    store.upsert_entity(EntityRef(entity_type="customer", display_name="客户甲"))
    store.upsert_entity(EntityRef(entity_type="supplier", display_name="供应商甲"))

    suppliers = store.entities("supplier")
    assert len(suppliers) == 1
    assert suppliers[0].display_name == "供应商甲"


# ── LLM 视图与画像 ──


def test_to_snapshot_renders_three_layers_and_tool_preview() -> None:
    """快照：L1 逐条文本（tool 结构化结果预览）、L2 时序拼接、画像直取。"""
    store = MemoryStore(session_id="s1", profile=UserProfile(summary_text="常用客户 CUS001"))
    store.add_raw("user", "查客户")
    store.add_raw("assistant", "好的")
    store.add_raw("tool", "", structured_result={"rows": [{"FBillNo": "SO001"}]})
    store.add_summary(_summary("第一轮摘要", 1, 1))
    store.add_summary(_summary("第二轮摘要", 2, 2))

    snapshot = store.to_snapshot()

    assert snapshot.layer1_recent[0] == "user: 查客户"
    assert snapshot.layer1_recent[1] == "assistant: 好的"
    assert "SO001" in snapshot.layer1_recent[2]  # tool 空文本用结构化预览
    assert snapshot.session_summary == "第一轮摘要\n第二轮摘要"  # 时序拼接
    assert snapshot.user_profile == "常用客户 CUS001"


def test_empty_store_snapshot_equals_default() -> None:
    """空记忆快照等于 M2 默认 MemorySnapshot（不破既有契约）。"""
    store = MemoryStore(session_id="s1")

    assert store.to_snapshot() == MemorySnapshot()


def test_update_profile_replaces_and_visible_in_snapshot() -> None:
    """长期画像整体替换，快照立即可见。"""
    store = MemoryStore(session_id="s1")
    assert store.get_profile() == UserProfile()

    store.update_profile(UserProfile(summary_text="画像更新", facts={"角色": "财务"}))

    assert store.get_profile().facts == {"角色": "财务"}
    assert store.to_snapshot().user_profile == "画像更新"
