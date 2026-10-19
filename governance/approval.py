"""审批单持久化与双人复核状态机（M9-T04）。

职责边界：
- ``FileApprovalStore``：一单一文件、原子写、0600、ID 白名单
- ``ApprovalService``：创建 / 查询 / 通过 / 拒绝 / 惰性过期 / 审计
- **不执行写操作**：approved 只是审批结论，执行恢复归 M9-T05

并发假设：进程内按单加锁，部署使用单 worker；跨进程锁归 M9-T11。
"""

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from loguru import logger
from pydantic import ValidationError

from config.approval_rules import (
    APPROVER_ROLES,
    DEFAULT_REQUIRED_APPROVALS,
    MAX_APPROVAL_COMMENT_CHARS,
)
from infrastructure.exceptions import ApprovalError
from models.approval import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalScope,
    ApprovalStatus,
    PendingAction,
)
from models.audit import AuditEvent, AuditEventType, AuditStatus
from models.user import UserContext
from observability.audit import AuditLogger
from safety.datasecurity import redact_text

_SCHEMA_VERSION = 1
_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_LIST_LIMIT = 200
_MAX_SUMMARY_CHARS = 2000


class FileApprovalStore:
    """审批单文件读写门面（一个实例对应一个目录）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """审批单目录（测试/展示用）。"""
        return self._root

    def path_for(self, approval_id: str) -> Path:
        """审批单文件路径；非法 ID 一律按不存在处理（防路径穿越）。"""
        if not _APPROVAL_ID_RE.fullmatch(approval_id):
            raise ApprovalError("非法审批 ID", code="approval_not_found")
        return self._root / f"{approval_id}.json"

    def save(self, action: PendingAction) -> Path:
        """原子写入审批单；文件 0600、目录 0700。"""
        path = self.path_for(action.approval_id)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "saved_at": datetime.now(UTC).isoformat(),
            "approval": action.model_dump(mode="json"),
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
            raise ApprovalError("审批单写入失败", code="approval_store_failed") from exc
        return path

    def load(self, approval_id: str) -> PendingAction:
        """读取并校验一张审批单（fail-closed）。"""
        path = self.path_for(approval_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ApprovalError("审批单不存在", code="approval_not_found") from exc
        except (OSError, ValueError) as exc:
            raise ApprovalError("审批单读取失败", code="approval_store_failed") from exc

        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ApprovalError("审批单结构不符", code="approval_store_failed")
        raw = payload.get("approval")
        if not isinstance(raw, dict):
            raise ApprovalError("审批单内容缺失", code="approval_store_failed")
        try:
            action = PendingAction.model_validate(raw)
        except ValidationError as exc:
            raise ApprovalError("审批单内容不合法", code="approval_store_failed") from exc
        if action.approval_id != approval_id:
            raise ApprovalError("审批单 ID 与文件名不一致", code="approval_store_failed")
        return action

    def list_all(self) -> list[PendingAction]:
        """列出全部合法审批单；坏文件跳过并告警。"""
        if not self._root.is_dir():
            return []
        actions: list[PendingAction] = []
        for path in sorted(self._root.glob("*.json")):
            try:
                actions.append(self.load(path.stem))
            except ApprovalError as exc:
                logger.warning("跳过损坏的审批单 | path={} error={}", path, exc)
        return actions

    def find_by_idempotency_key(
        self, requester_id: str, idempotency_key: str
    ) -> PendingAction | None:
        """按发起人 + 幂等键找最近一张单（用于创建幂等）。"""
        matches = [
            action
            for action in self.list_all()
            if action.requester_id == requester_id and action.idempotency_key == idempotency_key
        ]
        if not matches:
            return None
        return max(matches, key=lambda action: action.created_at)


class ApprovalService:
    """审批单状态机：双人复核、拒绝、惰性过期、幂等。"""

    def __init__(
        self,
        store: FileApprovalStore,
        *,
        audit: AuditLogger | None = None,
        ttl_seconds: int = 1800,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("审批有效期必须大于 0")
        self._store = store
        self._audit_logger = audit
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._create_lock = asyncio.Lock()

    async def create(
        self,
        *,
        trace_id: str,
        session_id: str,
        requester: UserContext,
        tool_name: str,
        arguments: Mapping[str, Any],
        summary: str,
        form_id: str = "",
        business_key: str = "",
        risk_score: float = 0.0,
        risk_level: str = "",
        required_approvers: Sequence[str] = (),
        idempotency_key: str,
    ) -> PendingAction:
        """创建待审批单；同发起人 + 幂等键重复调用返回原单。"""
        async with self._create_lock:
            existing = self._store.find_by_idempotency_key(requester.user_id, idempotency_key)
            if existing is not None:
                return existing

            now = self._now()
            action = PendingAction(
                trace_id=trace_id,
                session_id=session_id,
                requester_id=requester.user_id,
                requester_role=requester.role,
                tool_name=tool_name,
                summary=redact_text(summary)[:_MAX_SUMMARY_CHARS],
                form_id=form_id,
                business_key=business_key,
                risk_score=risk_score,
                risk_level=risk_level,
                required_approvers=list(required_approvers),
                required_approvals=DEFAULT_REQUIRED_APPROVALS,
                idempotency_key=idempotency_key,
                arguments=dict(arguments),
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            self._store.save(action)
            self._emit_audit(action, "approval.requested", requester, "success")
            return action

    async def get(self, approval_id: str, user: UserContext) -> PendingAction:
        """按 ID 取单；不存在/不可见统一 404，过期惰性落盘。"""
        async with self._lock_for(approval_id):
            action = self._store.load(approval_id)
            action = self._expire_if_needed(action)
            if not self._can_view(action, user):
                raise ApprovalError("审批单不存在", code="approval_not_found")
            return action

    async def list_for_user(
        self,
        user: UserContext,
        *,
        scope: ApprovalScope = "inbox",
        status: ApprovalStatus | None = None,
        limit: int = 50,
    ) -> list[PendingAction]:
        """按可见范围列出审批单；过期单先落盘再过滤。"""
        if scope == "all" and user.role != "admin":
            raise ApprovalError("无权查看全部审批单", code="approval_forbidden")

        actions: list[PendingAction] = []
        for candidate in self._store.list_all():
            async with self._lock_for(candidate.approval_id):
                try:
                    action = self._store.load(candidate.approval_id)
                except ApprovalError:
                    continue
                action = self._expire_if_needed(action)
                if not self._matches_scope(action, user, scope):
                    continue
                if status is not None and action.status != status:
                    continue
                actions.append(action)

        actions.sort(key=lambda action: action.created_at, reverse=True)
        return actions[: max(1, min(limit, _MAX_LIST_LIMIT))]

    async def approve(
        self, approval_id: str, user: UserContext, comment: str = ""
    ) -> PendingAction:
        """投一张通过票；两票后状态变 approved。"""
        return await self._decide(approval_id, user, comment, "approve")

    async def reject(self, approval_id: str, user: UserContext, comment: str = "") -> PendingAction:
        """拒绝审批单（拒绝优先）。"""
        return await self._decide(approval_id, user, comment, "reject")

    async def _decide(
        self,
        approval_id: str,
        user: UserContext,
        comment: str,
        decision: ApprovalDecisionType,
    ) -> PendingAction:
        async with self._lock_for(approval_id):
            action = self._store.load(approval_id)
            action = self._expire_if_needed(action)
            existing = next(
                (item for item in action.decisions if item.approver_id == user.user_id),
                None,
            )

            if action.status == "expired":
                raise ApprovalError("审批单已过期", code="approval_expired")
            if action.status == "approved":
                if decision == "approve" and existing is not None:
                    return action
                raise ApprovalError("审批单已通过，不能再次操作", code="approval_conflict")
            if action.status == "rejected":
                if decision == "reject" and existing is not None:
                    return action
                raise ApprovalError("审批单已拒绝，不能再次操作", code="approval_conflict")

            if user.user_id == action.requester_id:
                raise ApprovalError("发起人不能审批自己的单据", code="approval_forbidden")
            if user.role not in APPROVER_ROLES:
                raise ApprovalError("当前角色无审批权限", code="approval_forbidden")
            if existing is not None:
                if existing.decision == decision:
                    return action
                raise ApprovalError("已投过不同意见，不能重复操作", code="approval_conflict")

            now = self._now()
            clean_comment = redact_text(comment.strip())[:MAX_APPROVAL_COMMENT_CHARS]
            decisions = [
                *action.decisions,
                ApprovalDecision(
                    approver_id=user.user_id,
                    decision=decision,
                    comment=clean_comment,
                    decided_at=now,
                ),
            ]

            new_status: ApprovalStatus = "pending"
            if decision == "reject":
                new_status = "rejected"
            else:
                approved_count = sum(1 for item in decisions if item.decision == "approve")
                if approved_count >= action.required_approvals:
                    new_status = "approved"

            updated = action.model_copy(
                update={
                    "decisions": decisions,
                    "status": new_status,
                    "updated_at": now,
                }
            )
            self._store.save(updated)
            event_type: AuditEventType = (
                "approval.approved" if decision == "approve" else "approval.rejected"
            )
            audit_status: AuditStatus = "success" if decision == "approve" else "rejected"
            self._emit_audit(updated, event_type, user, audit_status)
            return updated

    def _expire_if_needed(self, action: PendingAction) -> PendingAction:
        """pending 且过期 → 落盘 expired；其他状态原样返回。"""
        if action.status != "pending" or self._now() < action.expires_at:
            return action
        updated = action.model_copy(update={"status": "expired", "updated_at": self._now()})
        self._store.save(updated)
        self._emit_audit(updated, "approval.expired", None, "blocked")
        return updated

    @staticmethod
    def _can_view(action: PendingAction, user: UserContext) -> bool:
        return action.requester_id == user.user_id or user.role in APPROVER_ROLES

    @staticmethod
    def _matches_scope(action: PendingAction, user: UserContext, scope: ApprovalScope) -> bool:
        if scope == "all":
            return True
        if scope == "mine":
            return action.requester_id == user.user_id
        return (
            action.status == "pending"
            and user.role in APPROVER_ROLES
            and action.requester_id != user.user_id
            and user.user_id not in {decision.approver_id for decision in action.decisions}
        )

    def _lock_for(self, approval_id: str) -> asyncio.Lock:
        lock = self._locks.get(approval_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[approval_id] = lock
        return lock

    def _now(self) -> datetime:
        now = self._clock()
        return now.replace(tzinfo=UTC) if now.tzinfo is None else now

    def _emit_audit(
        self,
        action: PendingAction,
        event_type: AuditEventType,
        actor: UserContext | None,
        status: AuditStatus,
    ) -> None:
        if self._audit_logger is None:
            return
        approved_count = sum(1 for decision in action.decisions if decision.decision == "approve")
        self._audit_logger.emit(
            AuditEvent(
                event_type=event_type,
                action="approval",
                trace_id=action.trace_id,
                user_id=actor.user_id if actor is not None else "",
                user_role=actor.role if actor is not None else "",
                tool_name=action.tool_name,
                risk_score=action.risk_score,
                risk_level=action.risk_level,
                approval_chain=[
                    f"{decision.approver_id}:{decision.decision}" for decision in action.decisions
                ],
                status=status,
                detail={
                    "approval_id": action.approval_id,
                    "status": action.status,
                    "required_approvals": action.required_approvals,
                    "approved_count": approved_count,
                },
            )
        )
