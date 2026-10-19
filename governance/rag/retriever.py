"""知识库检索服务（M9-T09）。

职责：在 T08 的 BM25 原语上补「问答需要的那一层」：
- 索引热加载（摄入后无需重启进程）
- BM25 top12 候选 → 规则重排 → top5
- 单文档条数上限，避免一份手册占满上下文
- 索引缺失/损坏降级为空索引（no_hit），不打断对话

边界：不调用 LLM、不做引用文案（引用策略在 ``governance/rag/answer.py``）。
"""

from collections import Counter
from pathlib import Path

from loguru import logger

from config.rag_rules import (
    KNOWLEDGE_NO_HIT_MESSAGE,
    RAG_ANSWER_TOP_K,
    RAG_INDEX_FILENAME,
    RAG_MAX_CHUNKS_PER_DOC,
    RAG_MAX_TOP_K,
    RAG_RETRIEVAL_TOP_K,
)
from governance.rag.index import RagIndex, tokenize_query
from infrastructure.exceptions import RagError
from models.rag import KnowledgeSearchResult, RagSearchHit


class KnowledgeRetriever:
    """基于本地索引的检索与轻量重排。"""

    def __init__(
        self,
        index_dir: Path,
        *,
        candidate_k: int = RAG_RETRIEVAL_TOP_K,
        answer_k: int = RAG_ANSWER_TOP_K,
        max_per_doc: int = RAG_MAX_CHUNKS_PER_DOC,
    ) -> None:
        self._index_path = index_dir / RAG_INDEX_FILENAME
        self._candidate_k = candidate_k
        self._answer_k = answer_k
        self._max_per_doc = max_per_doc
        self._cached: RagIndex | None = None
        self._fingerprint: tuple[int, int] | None = None

    def search(self, query: str, *, top_k: int | None = None) -> KnowledgeSearchResult:
        """检索并重排；无命中返回统一的无命中结果。"""
        limit = max(1, min(top_k or self._answer_k, RAG_MAX_TOP_K))
        candidates = self._index().search(query, top_k=self._candidate_k)
        hits = self._rerank(query, candidates, limit)
        return KnowledgeSearchResult(
            query=query,
            hits=hits,
            hit_count=len(hits),
            no_hit=not hits,
            message="" if hits else KNOWLEDGE_NO_HIT_MESSAGE,
        )

    # ────────────────────────── 索引热加载 ──────────────────────────

    def _index(self) -> RagIndex:
        """按 (mtime, size) 热加载；损坏时记 WARNING 并按空索引处理。"""
        try:
            stat = self._index_path.stat()
        except FileNotFoundError:
            self._cached = RagIndex.empty()
            self._fingerprint = None
            return self._cached

        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if fingerprint == self._fingerprint and self._cached is not None:
            return self._cached

        try:
            self._cached = RagIndex.load(self._index_path)
        except RagError as exc:
            # 问答侧优先可用性：摄入侧 T08 已 fail-closed，这里降级为「无命中」
            logger.warning("RAG 索引不可用，按空索引处理 | path={} error={}", self._index_path, exc)
            self._cached = RagIndex.empty()
        self._fingerprint = fingerprint
        return self._cached

    # ────────────────────────── 重排 ──────────────────────────

    def _rerank(self, query: str, candidates: list[RagSearchHit], limit: int) -> list[RagSearchHit]:
        """规则重排：标题精确命中 > 标题词重合 > 文档标题命中，再做单文档限流。"""
        normalized = query.strip().lower()
        query_terms = set(tokenize_query(query))
        scored: list[tuple[float, RagSearchHit]] = []
        for hit in candidates:
            heading_terms = set(tokenize_query(hit.heading))
            overlap = len(query_terms & heading_terms) / len(query_terms) if query_terms else 0.0
            bonus = 0.5 if normalized and normalized in hit.heading.lower() else 0.0
            bonus += 0.3 * overlap
            if hit.title and hit.title.lower() in normalized:
                bonus += 0.2
            scored.append((hit.score + bonus, hit))

        scored.sort(key=lambda item: (-item[0], item[1].chunk_id))
        per_document: Counter[str] = Counter()
        results: list[RagSearchHit] = []
        for score, hit in scored:
            if per_document[hit.doc_id] >= self._max_per_doc:
                continue
            per_document[hit.doc_id] += 1
            results.append(hit.model_copy(update={"score": round(score, 6)}))
            if len(results) >= limit:
                break
        return results
