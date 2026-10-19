"""只读计划的语义缓存（M7-T04，方案 §5.4 支柱十二）。

- 缓存对象：``PlanResult``（工具调用计划），**不缓存查询结果**
- 只读白名单：所有工具必须是 ``tool_type == "query"``，写/动作一律不缓存
- 键：问题归一化（去空白/标点/大小写）+ 可见工具集合 + 角色/数据域
- 结构：TTL + LRU，命中返回深拷贝，避免调用方污染缓存

Why 不做 embedding：本卡目标是高频重复查询命中，归一化键零额外调用、
零新依赖；跨措辞的语义相似度留待后续按真实命中率评估再引入。
"""

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence

from models.llm import PlanResult

_CACHE_VERSION = "v1"
# 去空白与标点（保留 CJK、字母、数字；下划线视为分隔符）
_NORMALIZE_RE = re.compile(r"[\s\W_]+")


def build_cache_key(
    user_input: str,
    *,
    tool_names: Sequence[str],
    scope: Mapping[str, object],
) -> str:
    """构造缓存键：归一化问题 + 可见工具 + 权限范围 + 版本号。"""
    payload = json.dumps(
        {
            "v": _CACHE_VERSION,
            "q": _NORMALIZE_RE.sub("", user_input.lower()),
            "tools": sorted(tool_names),
            "scope": scope,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_cacheable_plan(plan: PlanResult, tool_types: Mapping[str, str]) -> bool:
    """仅纯只读且非空的计划可缓存（未知工具按不可缓存处理）。"""
    if not plan.tool_calls:
        return False
    return all(tool_types.get(call.tool_name) == "query" for call in plan.tool_calls)


class SemanticCache:
    """TTL + LRU 的计划缓存（进程内，线程安全）。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("缓存 TTL 必须大于 0")
        if max_entries <= 0:
            raise ValueError("缓存容量必须大于 0")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, PlanResult]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        """当前缓存条数（测试/观测用）。"""
        with self._lock:
            return len(self._entries)

    def get(self, key: str) -> PlanResult | None:
        """命中返回深拷贝；过期条目删除并视为未命中。"""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, plan = entry
            if now - stored_at > self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return plan.model_copy(deep=True)

    def set(self, key: str, plan: PlanResult) -> None:
        """写入并执行 LRU 淘汰。"""
        with self._lock:
            self._entries[key] = (self._clock(), plan.model_copy(deep=True))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """清空缓存（测试/配置热切换用）。"""
        with self._lock:
            self._entries.clear()
