"""M3-T03：记忆压缩器单测（FakeProvider 零网络）。

覆盖：三触发条件判定、滚动窗口与绝对轮次换算、指代留存（验收核心）、
多次压缩范围不重叠、LLM 失败放行、窗口空放行。
"""

from agent.memory import MemoryStore
from agent.memory_compressor import (
    REASON_AGGRESSIVE,
    REASON_ROLLING,
    REASON_URGENT,
    compression_reason,
    maybe_compress,
)
from config.settings import Settings
from infrastructure.exceptions import LLMUnavailableError
from models.llm import EntityDraft, SummaryDraft
from tests.fakes import FakeProvider


def _settings(**overrides: object) -> Settings:
    # _env_file=None：防本机 .env 污染阈值（T03 教训：env_file 独立于 os.environ）
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _add_round(store: MemoryStore, index: int, *, text: str = "短") -> None:
    """补一轮 user+assistant（默认短文本，不碰 token 阈值线）。"""
    store.add_raw("user", f"第{index}轮问题{text}")
    store.add_raw("assistant", f"第{index}轮回复{text}")


# ── 触发判定（纯函数）──


def test_no_trigger_under_thresholds() -> None:
    """2 轮短对话：三条件都不触发。"""
    store = MemoryStore(session_id="s1")
    _add_round(store, 1)
    _add_round(store, 2)

    assert compression_reason(store, _settings()) is None


def test_rolling_trigger_when_rounds_exceed_keep() -> None:
    """8 轮（> keep 6）且 token 未超线：rolling（优先级最低但唯一命中）。"""
    store = MemoryStore(session_id="s1")
    for i in range(1, 9):
        _add_round(store, i)

    assert compression_reason(store, _settings()) == REASON_ROLLING


def test_aggressive_has_priority_over_rolling() -> None:
    """占用率超警戒线但未超总预算：aggressive（即使轮数也超 rolling 线）。"""
    store = MemoryStore(session_id="s1")
    # 警戒线 = 100000*0.001 = 100 token：16 条 × 约 45 CJK ≈ 720 token，踩线但远低总预算
    long_text = "这是一段足够长的业务对话内容用来撑过警戒 token 线但远低于总预算值"
    for i in range(1, 9):
        _add_round(store, i, text=long_text)

    settings = _settings(memory_token_budget=100000, memory_warning_ratio=0.001)
    assert compression_reason(store, settings) == REASON_AGGRESSIVE


def test_urgent_has_top_priority() -> None:
    """总需求超预算：urgent 优先于其余两条件。"""
    store = MemoryStore(session_id="s1")
    for i in range(1, 9):
        _add_round(store, i)

    settings = _settings(memory_token_budget=10, memory_warning_ratio=0.9)
    assert compression_reason(store, settings) == REASON_URGENT


# ── 压缩主流程 ──


async def test_rolling_compress_window_summary_and_eviction() -> None:
    """rolling 压缩：窗口=前 2 轮，摘要挂载（绝对轮次 1->2），原文只剩 6 轮。"""
    store = MemoryStore(session_id="s1")
    for i in range(1, 9):
        _add_round(store, i)
    provider = FakeProvider(
        summarize_queue=[SummaryDraft(summary_text="前两轮摘要", key_entities=[])]
    )

    summary = await maybe_compress(provider, store, _settings())

    assert summary is not None
    assert summary.compressed_from_round == 1
    assert summary.compressed_to_round == 2
    assert store.raw_rounds == 6  # 8 - 2
    assert len(store.summaries()) == 1
    # summarize 收到的窗口正是前两轮原文
    window_text = [m.content for m in provider.last_summarize_messages]
    assert "第1轮问题短" in window_text
    assert "第2轮回复短" in window_text
    assert all("第7轮" not in c for c in window_text)
    # 保留段从第 3 轮 user 消息开始
    assert store.raw_messages()[0].content == "第3轮问题短"


async def test_referential_info_survives_compression() -> None:
    """验收核心：CUS001/SO000008 经摘要文本+实体图+快照三处留存，原文可清除。"""
    store = MemoryStore(session_id="s1")
    store.add_raw("user", "查客户 CUS001 的订单")
    store.add_raw("tool", "", structured_result={"rows": [{"FBillNo": "SO000008"}]})
    store.add_raw("assistant", "查到订单 SO000008")
    for i in range(2, 9):  # 补到 8 轮触发 rolling
        _add_round(store, i)
    draft = SummaryDraft(
        summary_text="用户查询了客户 CUS001 的订单，订单号 SO000008",
        key_entities=[
            EntityDraft(entity_type="customer", display_name="客户 CUS001", kingdee_id="CUS001"),
            EntityDraft(
                entity_type="order", display_name="销售订单 SO000008", kingdee_id="SO000008"
            ),
        ],
    )

    summary = await maybe_compress(FakeProvider(summarize_queue=[draft]), store, _settings())

    assert summary is not None
    # ① 摘要文本保留指代
    assert "CUS001" in store.summaries()[0].summary_text
    assert "SO000008" in store.summaries()[0].summary_text
    # ② 实体图入图，提及轮次记到窗口末轮（绝对轮次 2）
    entities = {e.entity_type: e for e in store.entities()}
    assert entities["customer"].kingdee_id == "CUS001"
    assert entities["order"].last_mentioned_in_round == summary.compressed_to_round
    # ③ LLM 快照可见摘要（后续轮指代解析的输入）
    assert "CUS001" in store.to_snapshot().session_summary
    # ④ 窗口原文已清除（信息全部上浮，Layer 1 不留底）
    raw_text = " ".join(m.content for m in store.raw_messages())
    assert "CUS001" not in raw_text


async def test_repeated_compression_ranges_do_not_overlap() -> None:
    """两次压缩：第二次范围从 3 开始，与第一次 1->2 不重叠。"""
    store = MemoryStore(session_id="s1")
    for i in range(1, 9):
        _add_round(store, i)
    provider = FakeProvider(
        summarize_queue=[
            SummaryDraft(summary_text="第一次摘要 1-2"),
            SummaryDraft(summary_text="第二次摘要 3-10"),
        ]
    )
    settings = _settings()

    first = await maybe_compress(provider, store, settings)
    assert first is not None
    # 第一次后剩 6 轮（相对），再补 8 轮 → 相对 14 轮，窗口 8 轮
    for i in range(9, 17):
        _add_round(store, i)
    second = await maybe_compress(provider, store, settings)

    assert second is not None
    assert (second.compressed_from_round, second.compressed_to_round) == (3, 10)
    assert store.raw_rounds == 6  # 14 - 8
    assert [s.summary_text for s in store.summaries()] == ["第一次摘要 1-2", "第二次摘要 3-10"]


async def test_llm_failure_keeps_buffer_intact() -> None:
    """D4：summarize 抛 LLM 异常 → 返回 None、buffer 原样、不挂摘要。"""
    store = MemoryStore(session_id="s1")
    for i in range(1, 9):
        _add_round(store, i)
    provider = FakeProvider(summarize_queue=[LLMUnavailableError("压缩超时")])

    result = await maybe_compress(provider, store, _settings())

    assert result is None
    assert store.raw_rounds == 8
    assert len(store.summaries()) == 0
    assert provider.summarize_calls == 1


async def test_empty_window_when_rounds_below_keep_skips() -> None:
    """urgent/aggressive 触发但不足 keep 轮（窗口空）：放行且不调 LLM。"""
    store = MemoryStore(session_id="s1")
    _add_round(store, 1, text="很" * 200)  # 单轮撑爆预算
    provider = FakeProvider()
    settings = _settings(memory_token_budget=10, memory_warning_ratio=0.5)

    assert compression_reason(store, settings) == REASON_URGENT
    result = await maybe_compress(provider, store, settings)

    assert result is None
    assert provider.summarize_calls == 0  # 无窗口不调压缩 LLM
    assert store.raw_rounds == 1  # 原文不动
