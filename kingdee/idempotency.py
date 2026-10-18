"""幂等层（M5-T01）：Key 生成 + 存储去重 + TTL。

方案 §5.1 支柱四：同一请求（用户 + 工具/意图 + 参数指纹 + 时间桶）只执行一次。
写操作重试必须经过本层，禁止直接重放——这是 Saga 补偿与网络超时重试的前提。

设计要点：
- **Key 纯函数**：参数递归排序后 canonical JSON + SHA256；字典顺序不影响结果
- **时间桶**：``floor(now / ttl)``，桶内同 Key、跨桶换 Key（与 TTL 对齐）
- **并发去重**：服务持有 per-key ``asyncio.Lock`` + 双检锁，同进程并发只执行一次
- **失败可重试**：失败记录不缓存结果；下次同 Key 重新执行（是否重复由业务判断）
- **fail-closed**：同一 Key 处于 processing（异常残留/跨进程）时拒绝，不重复执行
- 存储抽象成 ``IdempotencyStore`` 协议，先内存实现，后续可换 SQLite/Redis
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar, cast

from config.settings import get_settings
from infrastructure.exceptions import IdempotencyError

_T = TypeVar("_T")

IdempotencyStatus = Literal["processing", "success", "failed"]


@dataclass(frozen=True)
class IdempotencyRecord:
    """一条幂等记录（状态 + 结果 + 过期时间）。"""

    key: str
    status: IdempotencyStatus
    expires_at: float
    result: Any = None
    error: str = ""


class IdempotencyStore(Protocol):
    """幂等记录存储（只负责读写，锁与流程在 Service）。"""

    def get(self, key: str, now: float) -> IdempotencyRecord | None:
        """取未过期记录；已过期返回 None（可顺手清理）。"""
        ...

    def put(self, record: IdempotencyRecord) -> None:
        """写入/覆盖记录（同 Key 状态迁移）。"""
        ...


class InMemoryIdempotencyStore:
    """进程内存储：单进程/单测够用；跨进程持久化留待 SQLite/Redis 实现。"""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}

    def get(self, key: str, now: float) -> IdempotencyRecord | None:
        record = self._records.get(key)
        if record is None:
            return None
        if record.expires_at <= now:
            # 懒过期：被访问时才清理，避免后台任务
            self._records.pop(key, None)
            return None
        return record

    def put(self, record: IdempotencyRecord) -> None:
        self._records[record.key] = record


def build_idempotency_key(
    user_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    time_bucket: int = 0,
) -> str:
    """生成确定性幂等 Key（参数顺序无关，SHA256 十六进制）。"""
    payload = {
        "user_id": user_id,
        "tool_name": tool_name,
        "arguments": arguments,
        "time_bucket": time_bucket,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyService:
    """幂等执行器：同 Key 只执行一次，成功结果直接复用。"""

    def __init__(
        self,
        store: IdempotencyStore | None = None,
        *,
        ttl_seconds: int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store or InMemoryIdempotencyStore()
        self._ttl = (
            ttl_seconds if ttl_seconds is not None else get_settings().idempotency_ttl_seconds
        )
        self._clock = clock or time.time
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def ttl_seconds(self) -> int:
        """去重窗口（测试/审计用）。"""
        return self._ttl

    def build_key(
        self,
        user_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> str:
        """按当前时间桶生成 Key（桶 = floor(now / ttl)）。"""
        bucket = int(self._clock() // self._ttl)
        return build_idempotency_key(user_id, tool_name, arguments, time_bucket=bucket)

    async def run(self, key: str, operation: Callable[[], Awaitable[_T]]) -> _T:
        """执行操作；同 Key 已成功则直接返回缓存结果。

        Args:
            key: 幂等 Key（Saga 场景用 ``saga_id:step_id`` 等确定性输入）。
            operation: 实际执行体（写操作/补偿操作）。

        Raises:
            IdempotencyError: 同 Key 处于 processing（异常残留或跨进程并发），
                为避免重复写操作直接拒绝。
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock()
            record = self._store.get(key, now)
            if record is not None and record.status == "success":
                return cast(_T, record.result)
            if record is not None and record.status == "processing":
                raise IdempotencyError(
                    "同一幂等 Key 正在处理中，拒绝重复执行",
                    context={"key": key},
                )

            expires_at = now + self._ttl
            self._store.put(IdempotencyRecord(key=key, status="processing", expires_at=expires_at))
            try:
                result = await operation()
            except Exception as exc:
                self._store.put(
                    IdempotencyRecord(
                        key=key,
                        status="failed",
                        expires_at=expires_at,
                        error=str(exc),
                    )
                )
                raise
            self._store.put(
                IdempotencyRecord(
                    key=key,
                    status="success",
                    expires_at=expires_at,
                    result=result,
                )
            )
            return result

    async def run_once(
        self,
        *,
        user_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        """便捷入口：按用户+工具+参数自动生成 Key。"""
        key = self.build_key(user_id, tool_name, arguments)
        return await self.run(key, operation)
