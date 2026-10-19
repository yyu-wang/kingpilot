"""本地 BM25 索引与检索（M9-T08）。

设计要点：
- **零新依赖**：中文用字符 unigram + bigram，英文/数字按词
- **不持久化倒排表**：``index.json`` 只存 documents + chunks，加载时现算
- **可复现排序**：分数降序，同分按 ``chunk_id`` 升序
- **fail-closed**：索引损坏 / 引用不完整一律 ``RagError(rag_index_invalid)``

T09 边界：本模块只提供检索原语，不负责引用格式与「知识库没有」话术。
"""

import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from config.rag_rules import (
    RAG_BM25_B,
    RAG_BM25_K1,
    RAG_DEFAULT_TOP_K,
    RAG_INDEX_SCHEMA_VERSION,
    RAG_MAX_TOP_K,
)
from infrastructure.exceptions import RagError
from models.rag import RagChunk, RagDocument, RagIndexPayload, RagSearchHit

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RUN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """文档侧分词：中文单字 + 相邻双字，英文/数字按词。

    Why 同时保留 unigram 与 bigram：
    - unigram 兜底单字查询（如「税」）
    - bigram 命中中文短语（如「销售订单」），避免单字噪声淹没长词
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for run in _CJK_RUN.findall(normalized):
        tokens.extend(run)  # 单字
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))  # 相邻双字
    tokens.extend(_LATIN_RUN.findall(normalized))
    return tokens


def tokenize_query(text: str) -> list[str]:
    """查询侧分词：中文多字串只用 bigram，单字串才用 unigram。

    Why 与文档侧不同：文档侧保留单字是为了支持「单字查询」；若查询也用单字，
    「报销流程」会因文档里出现「销售」的「销」字被误命中，噪声过大。
    """
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_LATIN_RUN.findall(normalized))
    return tokens


class RagIndex:
    """内存 BM25 索引（由 documents + chunks 构建）。"""

    def __init__(
        self,
        documents: Sequence[RagDocument] = (),
        chunks: Sequence[RagChunk] = (),
        *,
        source_root: str = "",
        built_at: datetime | None = None,
    ) -> None:
        self._documents = tuple(documents)
        self._chunks = tuple(chunks)
        self._source_root = source_root
        self._built_at = built_at or datetime.now(UTC)

        self._by_chunk_id: dict[str, RagChunk] = {}
        for chunk in self._chunks:
            if chunk.chunk_id in self._by_chunk_id:
                raise RagError("索引存在重复 chunk_id", code="rag_index_invalid")
            self._by_chunk_id[chunk.chunk_id] = chunk

        # 倒排表：term -> {chunk_id: tf}
        self._postings: dict[str, dict[str, int]] = defaultdict(dict)
        self._lengths: dict[str, int] = {}
        total_tokens = 0
        for chunk in self._chunks:
            tokens = tokenize(f"{chunk.heading}\n{chunk.content}")
            self._lengths[chunk.chunk_id] = len(tokens)
            total_tokens += len(tokens)
            for term, frequency in Counter(tokens).items():
                self._postings[term][chunk.chunk_id] = frequency

        self._chunk_count = len(self._chunks)
        self._avg_length = total_tokens / self._chunk_count if self._chunk_count else 0.0

    # ────────────────────────── 属性 ──────────────────────────

    @property
    def documents(self) -> tuple[RagDocument, ...]:
        """已入库文档元数据。"""
        return self._documents

    @property
    def chunks(self) -> tuple[RagChunk, ...]:
        """全部分块。"""
        return self._chunks

    @property
    def source_root(self) -> str:
        """索引对应的源目录（绝对路径）。"""
        return self._source_root

    @property
    def built_at(self) -> datetime:
        """索引构建时间。"""
        return self._built_at

    @classmethod
    def empty(cls) -> "RagIndex":
        """空索引：文件不存在时的安全默认值（T09 可据此回答「知识库没有」）。"""
        return cls()

    # ────────────────────────── 检索 ──────────────────────────

    def search(self, query: str, *, top_k: int = RAG_DEFAULT_TOP_K) -> list[RagSearchHit]:
        """BM25 检索；空查询 / 零命中返回空列表。"""
        terms = sorted(set(tokenize_query(query)))  # 查询去重，避免重复词重复加权
        if not terms or self._chunk_count == 0 or self._avg_length == 0:
            return []

        limit = max(1, min(top_k, RAG_MAX_TOP_K))
        scores: dict[str, float] = {}
        for term in terms:
            postings = self._postings.get(term)
            if not postings:  # 词不在库中：跳过（不参与 IDF）
                continue
            document_frequency = len(postings)
            idf = math.log(
                1 + (self._chunk_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for chunk_id, frequency in postings.items():
                length = self._lengths[chunk_id]
                denominator = frequency + RAG_BM25_K1 * (
                    1 - RAG_BM25_B + RAG_BM25_B * length / self._avg_length
                )
                scores[chunk_id] = scores.get(chunk_id, 0.0) + (
                    idf * frequency * (RAG_BM25_K1 + 1) / denominator
                )

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [self._to_hit(chunk_id, round(score, 6)) for chunk_id, score in ranked]

    def _to_hit(self, chunk_id: str, score: float) -> RagSearchHit:
        chunk = self._by_chunk_id[chunk_id]
        return RagSearchHit(
            chunk_id=chunk.chunk_id,
            doc_id=chunk.doc_id,
            path=chunk.path,
            title=chunk.title,
            heading=chunk.heading,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            score=score,
        )

    # ────────────────────────── 持久化 ──────────────────────────

    @classmethod
    def load(cls, path: Path) -> "RagIndex":
        """读取索引；文件不存在返回空索引，损坏则 fail-closed。"""
        if not path.exists():
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RagError("索引文件读取失败", code="rag_index_invalid") from exc
        try:
            payload = RagIndexPayload.model_validate(raw)
        except ValidationError as exc:
            raise RagError("索引文件结构非法", code="rag_index_invalid") from exc

        if payload.schema_version != RAG_INDEX_SCHEMA_VERSION:
            raise RagError("索引 schema 版本不兼容", code="rag_index_invalid")

        document_ids = {document.doc_id for document in payload.documents}
        if len(document_ids) != len(payload.documents):
            raise RagError("索引存在重复 doc_id", code="rag_index_invalid")
        for chunk in payload.chunks:
            if chunk.doc_id not in document_ids:
                raise RagError("分块引用了不存在的文档", code="rag_index_invalid")

        return cls(
            payload.documents,
            payload.chunks,
            source_root=payload.source_root,
            built_at=payload.built_at,
        )

    def save(self, path: Path) -> None:
        """原子写入索引：临时文件自检通过后才替换正式文件（0600）。"""
        if not self._source_root:
            raise RagError("索引缺少源目录，拒绝写入", code="rag_index_write_failed")

        payload = RagIndexPayload(
            schema_version=RAG_INDEX_SCHEMA_VERSION,
            built_at=self._built_at,
            source_root=self._source_root,
            documents=list(self._documents),
            chunks=list(self._chunks),
        )
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            temporary.write_text(
                json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            self.load(temporary)  # 写前自检：schema / 引用完整性
            temporary.replace(path)
            os.chmod(path, 0o600)
            self.load(path)  # 写后自检：确认落盘内容可读
        except RagError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise RagError("索引写入失败", code="rag_index_write_failed") from exc
