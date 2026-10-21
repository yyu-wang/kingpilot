"""M3-T07：会话持久化单测（tmp_path 真文件，零网络）。

覆盖：三层记忆 roundtrip（含工具结构化结果/摘要/实体）、原子覆盖写、
非法会话 ID 防路径穿越、损坏文件与 schema 不符 fail-closed、列表与删除。
"""

import json
from pathlib import Path

import pytest

from agent.memory import MemoryStore
from agent.session_store import SessionStore
from infrastructure.exceptions import SessionStoreError
from models.memory import ConversationSummary, EntityRef, UserProfile


def _memory_with_data(session_id: str = "sess-1") -> MemoryStore:
    memory = MemoryStore(session_id=session_id)
    memory.add_raw("user", "查客户 CUS001 的订单")
    memory.add_raw(
        "tool",
        "",
        structured_result={
            "form_id": "SAL_Order",
            "field_keys": ["FBillNo"],
            "rows": [{"FBillNo": "SO000001"}],
        },
    )
    memory.add_raw("assistant", "共 1 张订单", trace_id="trace-001")
    memory.add_raw("user", "第二笔呢")
    memory.add_raw("assistant", "只有一笔")
    memory.add_summary(
        ConversationSummary(
            summary_text="用户查询 CUS001 的订单",
            key_entities=(
                EntityRef(entity_type="customer", display_name="测试客户", kingdee_id="CUS001"),
            ),
            unresolved_questions=(),
            tool_results_cache={"order": "SO000001"},
            compressed_from_round=1,
            compressed_to_round=2,
            original_token_count=100,
            compressed_token_count=20,
        )
    )
    memory.upsert_entity(
        EntityRef(
            entity_type="order",
            display_name="SO000001",
            kingdee_id="SO000001",
            last_mentioned_in_round=2,
            attributes={"FDocumentStatus": "C", "FBillAllAmount": "45000.0000000000"},
        )
    )
    return memory


def test_roundtrip_restores_three_layers(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    original = _memory_with_data()
    store.save(original)

    loaded = store.load("sess-1")

    assert loaded.session_id == "sess-1"
    assert loaded.raw_rounds == 2
    assert loaded.raw_messages() == original.raw_messages()
    # Trace 链接随 assistant 消息持久化（控制台历史「查看 Trace」按钮）
    assert loaded.raw_messages()[2].trace_id == "trace-001"
    # 工具消息 content 为空时靠 structured_result 还原
    assert loaded.raw_messages()[1].content == ""
    assert loaded.raw_messages()[1].structured_result["rows"] == [{"FBillNo": "SO000001"}]
    assert loaded.summaries() == original.summaries()
    assert loaded.entities() == original.entities()


def test_save_overwrites_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    memory = _memory_with_data()
    store.save(memory)
    memory.add_raw("user", "第三轮")
    memory.add_raw("assistant", "第三轮答复")
    store.save(memory)

    loaded = store.load("sess-1")
    assert loaded.raw_rounds == 3
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_load_missing_returns_empty_session(tmp_path: Path) -> None:
    loaded = SessionStore(tmp_path).load("brand-new")
    assert loaded.session_id == "brand-new"
    assert loaded.raw_rounds == 0
    assert loaded.entities() == ()


@pytest.mark.parametrize("session_id", ["../evil", "a/b", "", "a" * 65, ".hidden", "a b"])
def test_invalid_session_id_rejected(tmp_path: Path, session_id: str) -> None:
    """路径穿越与非法字符一律拒绝（session_id 来自用户输入，系统边界必校验）。"""
    with pytest.raises(SessionStoreError, match="非法会话 ID"):
        SessionStore(tmp_path).path_for(session_id)


def test_corrupt_json_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "sess-1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionStoreError, match="读取失败"):
        SessionStore(tmp_path).load("sess-1")


def test_schema_version_mismatch_fail_closed(tmp_path: Path) -> None:
    payload = {
        "schema_version": 999,
        "session_id": "sess-1",
        "raw_messages": [],
        "summaries": [],
        "entities": [],
    }
    (tmp_path / "sess-1.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionStoreError, match="schema 版本不符"):
        SessionStore(tmp_path).load("sess-1")


def test_session_id_mismatch_fail_closed(tmp_path: Path) -> None:
    """文件内容声称的 session_id 与文件名不符 → 拒绝（防拷错/覆盖事故）。"""
    payload = {
        "schema_version": 1,
        "session_id": "other",
        "raw_messages": [],
        "summaries": [],
        "entities": [],
    }
    (tmp_path / "sess-1.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionStoreError, match="不一致"):
        SessionStore(tmp_path).load("sess-1")


def test_list_sessions_and_delete(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.save(_memory_with_data("sess-a"))
    store.save(_memory_with_data("sess-b"))
    (tmp_path / "broken.json").write_text("bad", encoding="utf-8")

    sessions = store.list_sessions()

    # 同一秒保存时按 ID 倒序兜底，保证最新在前的稳定顺序
    assert [(info.session_id, info.rounds) for info in sessions] == [
        ("sess-b", 2),
        ("sess-a", 2),
    ]
    assert all(info.title == "查客户 CUS001 的订单" for info in sessions)
    assert store.delete("sess-a") is True
    assert store.delete("sess-a") is False
    assert store.load("sess-a").raw_rounds == 0


def test_list_sessions_newest_first(tmp_path: Path) -> None:
    """会话列表按 saved_at 倒序：最近聊过的排最前。"""

    def _write(session_id: str, saved_at: str, question: str) -> None:
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "saved_at": saved_at,
            "raw_messages": [{"role": "user", "content": question}],
            "summaries": [],
            "entities": [],
        }
        (tmp_path / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    _write("old", "2026-10-01T08:00:00+00:00", "旧问题")
    _write("new", "2026-10-20T08:00:00+00:00", "新问题")

    assert [info.session_id for info in SessionStore(tmp_path).list_sessions()] == ["new", "old"]


def test_list_sessions_title_truncates_first_question(tmp_path: Path) -> None:
    """标题取首条用户提问并截断 20 字，换行/连续空白折叠成一个空格。"""
    memory = MemoryStore(session_id="sess-title")
    memory.add_raw("assistant", "先寒暄")
    memory.add_raw("user", "帮我\n查询   华东区所有客户的应收账款明细列表")
    SessionStore(tmp_path).save(memory)

    info = SessionStore(tmp_path).list_sessions()[0]

    assert info.title == "帮我 查询 华东区所有客户的应收账款明细…"


def test_load_keeps_injected_profile(tmp_path: Path) -> None:
    """UserProfile 不落盘（归 M7），但加载时可注入既有画像。"""
    profile = UserProfile(summary_text="常用客户 CUS001")
    loaded = SessionStore(tmp_path).load("sess-1", profile=profile)
    assert loaded.get_profile() == profile
