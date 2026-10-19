"""Badcase 持久化、标记与评测集导出（M9-T07）。

职责边界：
- ``FileBadcaseStore``：一单一个 JSON、原子写、0600、ID 白名单
- ``BadcaseService``：Trace → 脱敏 → DLP → 标记；标记 → 数据集原子合并
- **不删除/回滚**：评测集是发布门禁输入，本卡只做 append

并发假设：进程内按单 + 数据集加锁，部署使用单 worker；跨进程锁归 M9-T11。
"""

import asyncio
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from loguru import logger
from pydantic import ValidationError

from agent.tools import ToolRegistry
from config.badcase_rules import (
    BADCASE_REVIEWER_ROLES,
    DATASET_FILENAME,
    DATASET_VERSION_PATTERN,
    DEFAULT_DATASET_VERSION,
    MAX_BADCASE_CATEGORY_CHARS,
    MAX_BADCASE_QUESTION_CHARS,
    MAX_BADCASE_REASON_CHARS,
    MAX_EXPECTED_BEHAVIOR_CHARS,
)
from governance.evaluation.dataset import load_dataset
from infrastructure.exceptions import (
    BadcaseError,
    EvaluationError,
    TraceStoreError,
)
from models.agent import AgentTrace
from models.audit import AuditEvent, AuditEventType, AuditStatus
from models.badcase import BadcaseRecord, BadcaseStatus
from models.user import UserContext
from observability.audit import AuditLogger
from observability.tracing import TraceStore
from safety.datasecurity import redact_text
from safety.dlp import DlpScanner

_SCHEMA_VERSION = 1
_BADCASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_LIST_LIMIT = 200


class FileBadcaseStore:
    """Badcase 文件读写门面（一个实例对应一个目录）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Badcase 目录（测试/展示用）。"""
        return self._root

    def path_for(self, badcase_id: str) -> Path:
        """Badcase 文件路径；非法 ID 按不存在处理（防路径穿越）。"""
        if not _BADCASE_ID_RE.fullmatch(badcase_id):
            raise BadcaseError("非法 Badcase ID", code="badcase_not_found")
        return self._root / f"{badcase_id}.json"

    def save(self, record: BadcaseRecord) -> Path:
        """原子写入一条记录；文件 0600、目录 0700。"""
        path = self.path_for(record.badcase_id)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "saved_at": datetime.now(UTC).isoformat(),
            "badcase": record.model_dump(mode="json"),
        }
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            os.chmod(self._root, 0o700)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(tmp, 0o600)
            tmp.replace(path)
            os.chmod(path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            raise BadcaseError("Badcase 写入失败", code="badcase_store_failed") from exc
        return path

    def load(self, badcase_id: str) -> BadcaseRecord:
        """读取并校验一条记录（fail-closed）。"""
        path = self.path_for(badcase_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BadcaseError("Badcase 不存在", code="badcase_not_found") from exc
        except (OSError, ValueError) as exc:
            raise BadcaseError("Badcase 读取失败", code="badcase_store_failed") from exc

        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise BadcaseError("Badcase 结构不符", code="badcase_store_failed")
        raw = payload.get("badcase")
        if not isinstance(raw, dict):
            raise BadcaseError("Badcase 内容缺失", code="badcase_store_failed")
        try:
            record = BadcaseRecord.model_validate(raw)
        except ValidationError as exc:
            raise BadcaseError("Badcase 内容不合法", code="badcase_store_failed") from exc
        if record.badcase_id != badcase_id:
            raise BadcaseError("Badcase ID 与文件名不一致", code="badcase_store_failed")
        return record

    def list_all(self) -> list[BadcaseRecord]:
        """列出全部合法记录；坏文件跳过并告警。"""
        if not self._root.is_dir():
            return []
        records: list[BadcaseRecord] = []
        for path in sorted(self._root.glob("*.json")):
            try:
                records.append(self.load(path.stem))
            except BadcaseError as exc:
                logger.warning("跳过损坏的 Badcase 文件 | path={} error={}", path, exc)
        return records

    def find_by_trace_id(self, trace_id: str) -> BadcaseRecord | None:
        """按 Trace 找已有记录（同一 Trace 只建一张单）。"""
        return next(
            (record for record in self.list_all() if record.trace_id == trace_id),
            None,
        )


class BadcaseService:
    """Badcase 标记、查询与评测集导出。"""

    def __init__(
        self,
        store: FileBadcaseStore,
        *,
        trace_store: TraceStore,
        tools: ToolRegistry,
        dataset_root: Path,
        audit: AuditLogger | None = None,
        scanner: DlpScanner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._trace_store = trace_store
        self._known_tools = frozenset(spec.name for spec in tools.all_specs())
        self._dataset_root = dataset_root
        self._audit_logger = audit
        self._scanner = scanner or DlpScanner()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._mark_lock = asyncio.Lock()
        self._export_lock = asyncio.Lock()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @property
    def store(self) -> FileBadcaseStore:
        """底层存储（测试/展示用）。"""
        return self._store

    async def mark(
        self,
        *,
        trace_id: str,
        expected_tool: str | None,
        reason: str,
        category: str,
        expected_behavior: str,
        user: UserContext,
    ) -> BadcaseRecord:
        """从 Trace 提取问题并建单；同一 Trace 幂等返回原记录。"""
        self._require_reviewer(user)
        trace = self._load_trace(trace_id)

        question = redact_text(trace.user_input).strip()[:MAX_BADCASE_QUESTION_CHARS]
        if not question:
            raise BadcaseError("Trace 缺少可用的用户问题", code="badcase_invalid")

        clean_reason = redact_text(reason).strip()
        if not clean_reason:
            raise BadcaseError("原因不能为空", code="badcase_invalid")
        if len(clean_reason) > MAX_BADCASE_REASON_CHARS:
            raise BadcaseError("原因过长", code="badcase_invalid")

        clean_behavior = redact_text(expected_behavior).strip()
        if len(clean_behavior) > MAX_EXPECTED_BEHAVIOR_CHARS:
            raise BadcaseError("期望行为过长", code="badcase_invalid")

        clean_category = category.strip() or "badcase"
        if len(clean_category) > MAX_BADCASE_CATEGORY_CHARS:
            raise BadcaseError("分类过长", code="badcase_invalid")

        clean_tool = expected_tool.strip() if isinstance(expected_tool, str) else None
        if clean_tool is not None and clean_tool not in self._known_tools:
            raise BadcaseError("期望工具未注册", code="badcase_unknown_tool")

        # 先脱敏再 DLP：即使掩码规则漏掉，也不允许敏感文本进入评测集
        if self._scanner.scan("\n".join((question, clean_reason, clean_behavior))).blocked:
            raise BadcaseError("Badcase 文本仍包含敏感信息", code="badcase_sensitive")

        async with self._mark_lock:
            existing = self._store.find_by_trace_id(str(trace.trace_id))
            if existing is not None:
                return existing

            record = BadcaseRecord(
                trace_id=str(trace.trace_id),
                question=question,
                expected_tool=clean_tool,
                expected_behavior=clean_behavior,
                category=clean_category,
                reason=clean_reason,
                status="marked",
                created_by=user.user_id,
                created_at=self._now(),
            )
            self._store.save(record)
            self._emit_audit(
                "badcase.marked",
                record,
                user,
                detail={"badcase_id": record.badcase_id, "trace_id": record.trace_id},
            )
            return record

    async def get(self, badcase_id: str, user: UserContext) -> BadcaseRecord:
        """读取一条记录；不存在 → 404。"""
        self._require_reviewer(user)
        return self._store.load(badcase_id)

    async def list_records(
        self,
        user: UserContext,
        *,
        status: BadcaseStatus | None = None,
        limit: int = 50,
    ) -> list[BadcaseRecord]:
        """按创建时间倒序列出记录；坏文件跳过。"""
        self._require_reviewer(user)
        records = [
            record for record in self._store.list_all() if status is None or record.status == status
        ]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[: max(1, min(limit, _MAX_LIST_LIMIT))]

    async def export(
        self,
        badcase_id: str,
        *,
        dataset_version: str = DEFAULT_DATASET_VERSION,
        user: UserContext,
    ) -> BadcaseRecord:
        """把记录追加到版本化评测集；已导出幂等返回。"""
        self._require_reviewer(user)
        version = dataset_version.strip()
        if not re.fullmatch(DATASET_VERSION_PATTERN, version):
            raise BadcaseError("数据集版本非法", code="badcase_invalid")

        async with self._lock_for(badcase_id):
            record = self._store.load(badcase_id)
            if record.status == "exported":
                return record

            async with self._export_lock:
                path = self._dataset_root / version / DATASET_FILENAME
                payload, cases = self._load_dataset_payload(path, version)

                if any(
                    case.get("question") == record.question
                    and case.get("expected_tool") == record.expected_tool
                    for case in cases
                ):
                    raise BadcaseError(
                        "该问题与期望工具已存在于评测集",
                        code="badcase_duplicate",
                    )

                question_id = self._unique_question_id(record.badcase_id, cases)
                cases.append(
                    {
                        "question_id": question_id,
                        "question": record.question,
                        "expected_tool": record.expected_tool,
                        "category": record.category,
                        "expected_behavior": record.expected_behavior,
                        "source": f"badcase:{record.badcase_id}",
                    }
                )
                payload["cases"] = cases
                self._write_dataset(path, payload)

                exported_at = self._now()
                updated = record.model_copy(
                    update={
                        "status": "exported",
                        "exported_dataset": f"{version}/{DATASET_FILENAME}",
                        "exported_question_id": question_id,
                        "exported_at": exported_at,
                    }
                )
                self._store.save(updated)
                self._emit_audit(
                    "badcase.exported",
                    updated,
                    user,
                    detail={
                        "badcase_id": updated.badcase_id,
                        "trace_id": updated.trace_id,
                        "dataset_version": version,
                        "question_id": question_id,
                    },
                )
                return updated

    def _load_dataset_payload(
        self, path: Path, version: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """读取既有数据集或返回新数据集骨架；校验失败一律 409。"""
        if not path.exists():
            return {"version": version, "cases": []}, []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BadcaseError("评测集读取失败", code="badcase_dataset_conflict") from exc
        if not isinstance(payload, dict):
            raise BadcaseError("评测集结构非法", code="badcase_dataset_conflict")
        if str(payload.get("version") or "").strip() != version:
            raise BadcaseError("评测集版本与目标版本不一致", code="badcase_dataset_conflict")

        try:
            load_dataset(path, known_tools=self._known_tools)
        except EvaluationError as exc:
            raise BadcaseError("评测集校验失败", code="badcase_dataset_conflict") from exc

        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list):
            raise BadcaseError("评测集 cases 非法", code="badcase_dataset_conflict")
        cases = [case for case in raw_cases if isinstance(case, dict)]
        return payload, cases

    def _write_dataset(self, path: Path, payload: dict[str, Any]) -> None:
        """原子写 + 写前/写后自检（schema 合法才允许替换正式文件）。"""
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(tmp, 0o600)
            load_dataset(tmp, known_tools=self._known_tools)
            tmp.replace(path)
            os.chmod(path, 0o600)
            load_dataset(path, known_tools=self._known_tools)
        except EvaluationError as exc:
            raise BadcaseError("评测集自检失败", code="badcase_dataset_conflict") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise BadcaseError("评测集写入失败", code="badcase_store_failed") from exc

    @staticmethod
    def _unique_question_id(badcase_id: str, cases: list[dict[str, Any]]) -> str:
        """``BC-{id[:8]}``；冲突时追加 ``-2``、``-3``。"""
        existing = {
            str(case.get("question_id"))
            for case in cases
            if isinstance(case.get("question_id"), str)
        }
        base = f"BC-{badcase_id[:8]}"
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _load_trace(self, trace_id: str) -> AgentTrace:
        try:
            return self._trace_store.load(trace_id)
        except TraceStoreError as exc:
            raise BadcaseError("Trace 不存在", code="badcase_not_found") from exc

    @staticmethod
    def _require_reviewer(user: UserContext) -> None:
        if user.role not in BADCASE_REVIEWER_ROLES:
            raise BadcaseError("当前角色无 Badcase 操作权限", code="forbidden")

    def _lock_for(self, badcase_id: str) -> asyncio.Lock:
        lock = self._locks.get(badcase_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[badcase_id] = lock
        return lock

    def _now(self) -> datetime:
        now = self._clock()
        return now.replace(tzinfo=UTC) if now.tzinfo is None else now

    def _emit_audit(
        self,
        event_type: AuditEventType,
        record: BadcaseRecord,
        user: UserContext,
        *,
        detail: dict[str, Any],
    ) -> None:
        if self._audit_logger is None:
            return
        status: AuditStatus = "success"
        self._audit_logger.emit(
            AuditEvent(
                event_type=event_type,
                action="badcase",
                trace_id=record.trace_id,
                user_id=user.user_id,
                user_role=user.role,
                status=status,
                detail=detail,
            )
        )
