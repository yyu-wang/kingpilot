"""知识库摄入与增量更新（M9-T08）。

流程：
1. 扫描源目录（确定性排序）
2. 文件安全过滤（隐藏项/符号链接/后缀/大小/编码）
3. 分级门禁（只允许 public / internal）
4. DLP 门禁（命中 PII 整篇拒绝，不做脱敏入库）
5. 按「相对路径 + 内容 SHA-256」与旧索引对比，复用未变化文档的 chunk
6. 有变化才原子写索引，并写一条 ``rag.indexed`` 审计事件

边界：
- 单篇文档失败只进报告，不阻塞整库
- 索引损坏 fail-closed（``rag_index_invalid``），由运维删除后重建
- 单进程写假设；跨进程锁归 M9-T11
"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from config.data_security import DataLevel
from config.rag_rules import (
    RAG_INDEX_FILENAME,
    RAG_INGEST_ALLOWED_LEVELS,
    RAG_LEVEL_BY_DIR,
    RAG_MAX_FILE_BYTES,
    RAG_SUPPORTED_SUFFIXES,
)
from governance.rag.chunking import chunk_document, extract_title
from governance.rag.index import RagIndex
from infrastructure.exceptions import RagError
from models.audit import AuditEvent
from models.rag import (
    RagChunk,
    RagDocument,
    RagIngestReport,
    RagSkippedFile,
    RagSkipReason,
)
from observability.audit import AuditLogger
from safety.dlp import DlpScanner


class RagIngestor:
    """目录摄入器：一次 ``refresh`` = 一次全量扫描 + 增量落盘。"""

    def __init__(
        self,
        *,
        source_root: Path,
        index_dir: Path,
        scanner: DlpScanner | None = None,
        audit: AuditLogger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source_root = source_root.resolve()
        self._index_path = index_dir / RAG_INDEX_FILENAME
        self._scanner = scanner or DlpScanner()
        self._audit_logger = audit
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def source_root(self) -> Path:
        """规范化后的源目录。"""
        return self._source_root

    @property
    def index_path(self) -> Path:
        """索引文件路径。"""
        return self._index_path

    def load_index(self) -> RagIndex:
        """加载当前索引；不存在返回空索引。"""
        return RagIndex.load(self._index_path)

    def refresh(self) -> RagIngestReport:
        """扫描源目录并增量更新索引，返回结构化报告。"""
        if not self._source_root.is_dir():
            raise RagError("知识库源目录不存在", code="rag_source_missing")

        index_exists = self._index_path.exists()
        previous = RagIndex.load(self._index_path)
        previous_paths = {document.path for document in previous.documents}
        previous_docs, previous_chunks = self._previous_state(previous)

        report = RagIngestReport()
        documents: list[RagDocument] = []
        chunks: list[RagChunk] = []
        seen_paths: set[str] = set()

        for path in self._iter_files():
            relative = path.relative_to(self._source_root).as_posix()
            if _is_hidden(relative):
                continue  # 隐藏文件/目录（.DS_Store 等）静默忽略

            skip_reason = self._skip_reason(path, relative)
            if skip_reason is not None:
                report.skipped.append(RagSkippedFile(path=relative, reason=skip_reason))
                continue

            try:
                payload = path.read_bytes()
            except OSError:
                report.skipped.append(RagSkippedFile(path=relative, reason="read_error"))
                continue

            if len(payload) > RAG_MAX_FILE_BYTES:
                report.skipped.append(RagSkippedFile(path=relative, reason="too_large"))
                continue

            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                report.skipped.append(RagSkippedFile(path=relative, reason="unsupported_encoding"))
                continue

            # 分级门禁：敏感目录整篇拒绝，不允许进入索引
            level = _classify_level(relative)
            if level not in RAG_INGEST_ALLOWED_LEVELS:
                report.skipped.append(RagSkippedFile(path=relative, reason="level_denied"))
                continue

            # DLP 门禁：命中即整篇拒绝；报告只留路径与原因，不留原值
            if self._scanner.scan(text).blocked:
                report.skipped.append(RagSkippedFile(path=relative, reason="sensitive_content"))
                continue

            digest = hashlib.sha256(payload).hexdigest()
            doc_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
            previous_document = previous_docs.get(relative)

            if (
                previous_document is not None
                and previous_document.content_hash == digest
                and previous_document.level == level
            ):
                # 未变化：复用旧文档与旧 chunks，保证 chunk_id 稳定
                documents.append(previous_document)
                chunks.extend(previous_chunks.get(previous_document.doc_id, []))
                report.unchanged += 1
                seen_paths.add(relative)
                continue

            title = extract_title(text, fallback=path.stem)
            document_chunks = chunk_document(
                text,
                doc_id=doc_id,
                path=relative,
                title=title,
            )
            if not document_chunks:
                report.skipped.append(RagSkippedFile(path=relative, reason="empty_document"))
                continue

            documents.append(
                RagDocument(
                    doc_id=doc_id,
                    path=relative,
                    title=title,
                    level=level,
                    content_hash=digest,
                    size_bytes=len(payload),
                    chunk_count=len(document_chunks),
                )
            )
            chunks.extend(document_chunks)
            if previous_document is None:
                report.added += 1
            else:
                report.updated += 1
            seen_paths.add(relative)

        report.removed = sum(1 for path in previous_paths if path not in seen_paths)
        report.chunks = len(chunks)

        if not index_exists or report.added or report.updated or report.removed:
            built_at = self._now()
            RagIndex(
                documents,
                chunks,
                source_root=str(self._source_root),
                built_at=built_at,
            ).save(self._index_path)
            report.written = True
            self._emit_audit(report)
            logger.info(
                "RAG 索引已更新 | added={} updated={} removed={} unchanged={} chunks={} skipped={}",
                report.added,
                report.updated,
                report.removed,
                report.unchanged,
                report.chunks,
                len(report.skipped),
            )
        else:
            logger.info(
                "RAG 索引无变化，跳过写入 | unchanged={} chunks={} skipped={}",
                report.unchanged,
                report.chunks,
                len(report.skipped),
            )
        return report

    # ────────────────────────── 内部 ──────────────────────────

    def _iter_files(self) -> list[Path]:
        """确定性遍历：按相对路径排序，保证报告与索引顺序可复现。"""
        return sorted(
            (path for path in self._source_root.rglob("*") if path.is_file() or path.is_symlink()),
            key=lambda path: path.relative_to(self._source_root).as_posix(),
        )

    def _previous_state(
        self, previous: RagIndex
    ) -> tuple[dict[str, RagDocument], dict[str, list[RagChunk]]]:
        """取旧索引状态；源目录变了视为换库，旧状态全部作废。"""
        if previous.source_root:
            previous_root = Path(previous.source_root)
            if previous_root.resolve() != self._source_root:
                logger.warning(
                    "RAG 源目录已变化，执行全量重建 | old={} new={}",
                    previous_root,
                    self._source_root,
                )
                return {}, {}

        documents = {document.path: document for document in previous.documents}
        chunks: dict[str, list[RagChunk]] = {}
        for chunk in previous.chunks:
            chunks.setdefault(chunk.doc_id, []).append(chunk)
        return documents, chunks

    @staticmethod
    def _skip_reason(path: Path, relative: str) -> RagSkipReason | None:
        """文件级门禁：符号链接 / 后缀。返回 None 表示可继续读取。"""
        if path.is_symlink():
            return "symlink"
        if path.suffix.lower() not in RAG_SUPPORTED_SUFFIXES:
            return "unsupported_suffix"
        return None

    def _now(self) -> datetime:
        now = self._clock()
        return now.replace(tzinfo=UTC) if now.tzinfo is None else now

    def _emit_audit(self, report: RagIngestReport) -> None:
        """摄入审计：只放聚合数字与源目录，不放任何文档内容/标题。"""
        if self._audit_logger is None:
            return
        self._audit_logger.emit(
            AuditEvent(
                event_type="rag.indexed",
                action="rag",
                status="success",
                detail={
                    "source_root": str(self._source_root),
                    "added": report.added,
                    "updated": report.updated,
                    "removed": report.removed,
                    "unchanged": report.unchanged,
                    "chunks": report.chunks,
                    "skipped": len(report.skipped),
                },
            )
        )


def _is_hidden(relative: str) -> bool:
    """任意层级以 ``.`` 开头的文件/目录都忽略。"""
    return any(part.startswith(".") for part in relative.split("/"))


def _classify_level(relative: str) -> DataLevel:
    """按一级目录名判定分级；没有等级目录的文档默认 internal。"""
    head = relative.split("/", 1)[0].lower()
    return RAG_LEVEL_BY_DIR.get(head, "internal")
