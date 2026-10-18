"""死信队列（M6-T06）：持久化失败事务 + 人工重放入口。

入队条件：Saga 最终 ``FAILED`` 且至少有一个正向步骤已成功——说明可能
产生了残留数据，靠自动补偿已经无法收口，必须人工核对。

重放：调用方注入 ``replay_fn``（修复依赖后重新执行），成功则标记 resolved；
失败保留 pending 并记录错误与尝试次数，便于持续人工处理。
"""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from loguru import logger

from infrastructure.exceptions import DeadLetterError
from kingdee.saga import SagaResult
from models.base import SagaStatus
from models.dead_letter import DeadLetter

_SCHEMA_VERSION = 1
_DL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

ReplayFn = Callable[[DeadLetter], Awaitable[Any]]


class DeadLetterStore(Protocol):
    """死信存储接口。"""

    def save(self, letter: DeadLetter) -> None: ...

    def get(self, dl_id: str) -> DeadLetter: ...

    def list_pending(self) -> list[DeadLetter]: ...

    def list_all(self) -> list[DeadLetter]: ...


class FileDeadLetterStore:
    """一封信一个 JSON 文件（原子写；查询时坏文件跳过）。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, dl_id: str) -> Path:
        if not _DL_ID_RE.match(dl_id):
            raise DeadLetterError("死信 ID 非法", context={"dl_id": dl_id})
        return self._directory / f"{dl_id}.json"

    def save(self, letter: DeadLetter) -> None:
        path = self.path_for(letter.dl_id)
        payload = {"schema_version": _SCHEMA_VERSION, "letter": letter.to_dict()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise DeadLetterError("死信写入失败", context={"dl_id": letter.dl_id}) from exc

    def get(self, dl_id: str) -> DeadLetter:
        path = self.path_for(dl_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DeadLetterError("死信不存在", context={"dl_id": dl_id}) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise DeadLetterError("死信读取失败或文件损坏", context={"dl_id": dl_id}) from exc

        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise DeadLetterError("死信 schema 版本不符", context={"dl_id": dl_id})
        payload = raw.get("letter")
        if not isinstance(payload, dict):
            raise DeadLetterError("死信内容缺失", context={"dl_id": dl_id})
        try:
            letter = DeadLetter.model_validate(payload)
        except Exception as exc:  # Pydantic 细节不泄露到调用方
            raise DeadLetterError("死信内容不合法", context={"dl_id": dl_id}) from exc
        if letter.dl_id != dl_id:
            raise DeadLetterError("死信 ID 与文件名不一致", context={"dl_id": dl_id})
        return letter

    def list_all(self) -> list[DeadLetter]:
        if not self._directory.exists():
            return []
        letters: list[DeadLetter] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                letters.append(self.get(path.stem))
            except DeadLetterError as exc:
                logger.warning("跳过损坏死信文件 | file={} error={}", path.name, exc)
        return sorted(letters, key=lambda item: item.created_at, reverse=True)

    def list_pending(self) -> list[DeadLetter]:
        return [letter for letter in self.list_all() if letter.status == "pending"]


class DeadLetterService:
    """死信入队、查询与人工重放。"""

    def __init__(
        self,
        store: DeadLetterStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def enqueue_from_saga(
        self,
        saga: SagaResult,
        *,
        trace_id: str = "",
        request: Mapping[str, Any] | None = None,
        manual_action: str = "",
    ) -> DeadLetter | None:
        """失败且可能残留数据的 Saga → 死信；无需人工时返回 None。"""
        if not _needs_manual(saga):
            return None

        letter = DeadLetter(
            dl_id=uuid4().hex,
            saga_id=saga.saga_id,
            trace_id=trace_id or saga.trace_id,
            created_at=self._clock(),
            original_error=saga.error or _first_step_error(saga),
            compensation_error=_compensation_error(saga),
            manual_action=manual_action or _default_manual_action(saga),
            request=dict(request or {}),
            saga_snapshot=saga.to_dict(),
        )
        self._store.save(letter)
        logger.warning(
            "写事务进入死信队列 | dl_id={} saga_id={} error={}",
            letter.dl_id,
            letter.saga_id,
            letter.original_error,
        )
        return letter

    async def replay(
        self,
        dl_id: str,
        replay_fn: ReplayFn,
        *,
        note: str = "重放成功",
    ) -> DeadLetter:
        """人工修复后重放；成功标记 resolved，失败保留 pending。"""
        letter = self._store.get(dl_id)
        if letter.status == "resolved":
            raise DeadLetterError("死信已解决，不能重复重放", context={"dl_id": dl_id})

        try:
            await replay_fn(letter)
        except Exception as exc:
            updated = letter.model_copy(
                update={
                    "replay_attempts": letter.replay_attempts + 1,
                    "last_replay_error": str(exc),
                }
            )
            self._store.save(updated)
            raise DeadLetterError("死信重放失败", context={"dl_id": dl_id}) from exc

        resolved = letter.model_copy(
            update={
                "status": "resolved",
                "resolved_at": self._clock(),
                "replay_attempts": letter.replay_attempts + 1,
                "last_replay_error": "",
                "resolution_note": note,
            }
        )
        self._store.save(resolved)
        logger.info("死信重放成功 | dl_id={} saga_id={}", dl_id, letter.saga_id)
        return resolved

    def mark_resolved(self, dl_id: str, note: str = "人工处理完成") -> DeadLetter:
        """人工确认已处理（不走自动重放）。"""
        letter = self._store.get(dl_id)
        resolved = letter.model_copy(
            update={
                "status": "resolved",
                "resolved_at": self._clock(),
                "resolution_note": note,
            }
        )
        self._store.save(resolved)
        return resolved

    def list_pending(self) -> list[DeadLetter]:
        return self._store.list_pending()


def _needs_manual(saga: SagaResult) -> bool:
    """只有「失败且已有成功步骤」才可能残留数据，需要人工介入。"""
    if saga.status != SagaStatus.FAILED:
        return False
    return any(step.status == SagaStatus.SUCCESS for step in saga.step_results)


def _first_step_error(saga: SagaResult) -> str:
    for step in saga.step_results:
        if step.status == SagaStatus.FAILED and step.error:
            return step.error
    return ""


def _compensation_error(saga: SagaResult) -> str:
    parts = [
        (
            f"{item.tool_name}({item.step_id}): {item.error}"
            if item.error
            else f"{item.tool_name}({item.step_id}): 需人工介入"
        )
        for item in saga.compensation_results
        if item.status == SagaStatus.FAILED or item.manual_intervention
    ]
    return "；".join(parts)


def _default_manual_action(saga: SagaResult) -> str:
    if any(item.manual_intervention for item in saga.compensation_results):
        return "存在无法自动补偿的步骤，请人工核对并回滚/补录"
    return "补偿失败，请人工核对残留数据"
