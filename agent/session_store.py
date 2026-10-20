"""会话持久化（M3-T07，方案 §5.3 三层记忆的本地落盘）。

一个会话一个 JSON 文件（``{root}/{session_id}.json``），保存三层记忆：
Layer 1 原文 / Layer 2 摘要 / Layer 3 实体图。跨会话长期画像（UserProfile）
不在本文件范围（T02 已注明演化与持久化归 M7）。

Why JSON 而非 SQLite：会话是单机小体量数据、无查询需求；SQLite 的 schema
归 M5 账本 / M6 可观测性，此处提前建表会在后续任务重复迁移。

可靠性：
- **原子写**：先写 ``.tmp`` 再 ``replace``，进程崩溃不会留下半截文件
- **路径安全**：session_id 白名单校验，杜绝 ``../`` 路径穿越
- **fail-closed**：文件损坏 / schema 版本不符 / 文件名与内容不一致 → 抛
  ``SessionStoreError``，不静默丢历史
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from agent.memory import MemoryStore
from infrastructure.exceptions import SessionStoreError
from models.memory import ConversationSummary, EntityRef, RawMessage, RawRole, UserProfile

_SCHEMA_VERSION = 1
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VALID_ROLES = frozenset({"user", "assistant", "tool"})


@dataclass(frozen=True)
class SessionInfo:
    """会话列表条目（``--list`` 展示用）。"""

    session_id: str
    rounds: int
    updated_at: str


class SessionStore:
    """会话文件读写门面（一个实例对应一个存储目录）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """存储根目录（测试与 CLI 展示用）。"""
        return self._root

    def path_for(self, session_id: str) -> Path:
        """会话文件路径；非法 ID 直接拒绝（防路径穿越）。"""
        _validate_session_id(session_id)
        return self._root / f"{session_id}.json"

    def save(self, memory: MemoryStore) -> Path:
        """把会话三层记忆原子写入磁盘，返回文件路径。"""
        path = self.path_for(memory.session_id)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": memory.session_id,
            "saved_at": _utc_now(),
            "raw_messages": [_dump_raw(message) for message in memory.raw_messages()],
            "summaries": [_dump_summary(summary) for summary in memory.summaries()],
            "entities": [_dump_entity(entity) for entity in memory.entities()],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise SessionStoreError(
                "会话写入失败", context={"session_id": memory.session_id, "path": str(path)}
            ) from exc
        return path

    def load(self, session_id: str, *, profile: UserProfile | None = None) -> MemoryStore:
        """加载会话；文件不存在时返回空会话（首次对话场景）。"""
        path = self.path_for(session_id)
        memory = MemoryStore(session_id=session_id, profile=profile)
        if not path.exists():
            return memory
        payload = self._read_payload(path, session_id)
        try:
            for item in payload["raw_messages"]:
                memory.add_raw(
                    _load_role(item),
                    str(item.get("content") or ""),
                    structured_result=item.get("structured_result") or {},
                    trace_id=str(item.get("trace_id") or ""),
                )
            for item in payload["summaries"]:
                memory.add_summary(_load_summary(item))
            for item in payload["entities"]:
                memory.upsert_entity(_load_entity(item))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SessionStoreError(
                "会话文件结构不符",
                context={"session_id": session_id, "path": str(path), "reason": type(exc).__name__},
            ) from exc
        return memory

    def list_sessions(self) -> list[SessionInfo]:
        """列出全部会话（损坏文件跳过并告警，不阻塞列表）。"""
        if not self._root.is_dir():
            return []
        sessions: list[SessionInfo] = []
        for path in sorted(self._root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                raw_messages = payload["raw_messages"]
                sessions.append(
                    SessionInfo(
                        session_id=str(payload["session_id"]),
                        rounds=sum(1 for item in raw_messages if item.get("role") == "user"),
                        updated_at=str(payload.get("saved_at") or ""),
                    )
                )
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                logger.warning("跳过无法解析的会话文件: path={} error={}", path, exc)
        return sessions

    def delete(self, session_id: str) -> bool:
        """删除会话文件；不存在返回 False。"""
        path = self.path_for(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    @staticmethod
    def _read_payload(path: Path, session_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SessionStoreError(
                "会话文件读取失败", context={"session_id": session_id, "path": str(path)}
            ) from exc
        if not isinstance(payload, dict):
            raise SessionStoreError(
                "会话文件结构不符", context={"session_id": session_id, "path": str(path)}
            )
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise SessionStoreError(
                "会话文件 schema 版本不符",
                context={
                    "session_id": session_id,
                    "expected": _SCHEMA_VERSION,
                    "actual": payload.get("schema_version"),
                },
            )
        if payload.get("session_id") != session_id:
            raise SessionStoreError(
                "会话文件与请求的会话 ID 不一致",
                context={"session_id": session_id, "path": str(path)},
            )
        return payload


# ── 序列化 ──


def _dump_raw(message: RawMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "token_count": message.token_count,
        "structured_result": message.structured_result,
        "trace_id": message.trace_id,
    }


def _dump_summary(summary: ConversationSummary) -> dict[str, Any]:
    return {
        "summary_text": summary.summary_text,
        "key_entities": [_dump_entity(entity) for entity in summary.key_entities],
        "unresolved_questions": list(summary.unresolved_questions),
        "tool_results_cache": summary.tool_results_cache,
        "compressed_from_round": summary.compressed_from_round,
        "compressed_to_round": summary.compressed_to_round,
        "original_token_count": summary.original_token_count,
        "compressed_token_count": summary.compressed_token_count,
    }


def _dump_entity(entity: EntityRef) -> dict[str, Any]:
    return {
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "kingdee_id": entity.kingdee_id,
        "last_mentioned_in_round": entity.last_mentioned_in_round,
        "attributes": entity.attributes,
    }


def _load_role(item: dict[str, Any]) -> RawRole:
    role = str(item["role"])
    if role not in _VALID_ROLES:
        raise ValueError(f"未知记忆角色: {role}")
    return cast(RawRole, role)  # 已用 _VALID_ROLES 收窄


def _load_summary(item: dict[str, Any]) -> ConversationSummary:
    return ConversationSummary(
        summary_text=str(item["summary_text"]),
        key_entities=tuple(_load_entity(entity) for entity in item["key_entities"]),
        unresolved_questions=tuple(str(question) for question in item["unresolved_questions"]),
        tool_results_cache=dict(item["tool_results_cache"]),
        compressed_from_round=int(item["compressed_from_round"]),
        compressed_to_round=int(item["compressed_to_round"]),
        original_token_count=int(item["original_token_count"]),
        compressed_token_count=int(item["compressed_token_count"]),
    )


def _load_entity(item: dict[str, Any]) -> EntityRef:
    return EntityRef(
        entity_type=str(item["entity_type"]),
        display_name=str(item["display_name"]),
        kingdee_id=str(item.get("kingdee_id") or ""),
        last_mentioned_in_round=int(item.get("last_mentioned_in_round") or 0),
        attributes=dict(item.get("attributes") or {}),
    )


# ── 辅助 ──


def _validate_session_id(session_id: str) -> None:
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise SessionStoreError(
            "非法会话 ID",
            context={
                "session_id": session_id,
                "hint": "仅允许字母数字开头、字母数字与 ._- 组成、长度 ≤64",
            },
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
