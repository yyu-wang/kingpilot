"""RAG 数据模型（M9-T08）。

职责：
- 文档、分块、检索命中、摄入报告、索引文件 payload 的统一模型
- 全部继承 ``KingPilotBaseModel``（``extra="forbid"``，fail-closed）

边界：
- 索引内只保留相对路径与文本内容，不保留任何绝对路径（除 ``source_root``）
- 跳过原因只记录类别，不记录敏感原值
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from config.data_security import DataLevel
from models.base import KingPilotBaseModel

# 单篇文档被跳过的原因闭集（报告与测试共用）
RagSkipReason = Literal[
    "level_denied",
    "sensitive_content",
    "too_large",
    "unsupported_encoding",
    "unsupported_suffix",
    "empty_document",
    "read_error",
    "symlink",
]


class RagDocument(KingPilotBaseModel):
    """一篇已入库文档的元数据（不含正文）。"""

    doc_id: str = Field(min_length=1, max_length=32, description="相对路径的 SHA-256 前 16 位")
    path: str = Field(min_length=1, max_length=1024, description="相对源目录的 POSIX 路径")
    title: str = Field(default="", max_length=500, description="首个 H1，缺省为文件名 stem")
    level: DataLevel = Field(description="文档分级（由一级目录名判定）")
    content_hash: str = Field(min_length=64, max_length=64, description="原始字节 SHA-256")
    size_bytes: int = Field(ge=0, description="原始字节数")
    chunk_count: int = Field(ge=0, description="该文档的分块数")


class RagChunk(KingPilotBaseModel):
    """一个可检索的最小知识单元。"""

    chunk_id: str = Field(min_length=1, max_length=64, description="{doc_id}-{序号:04d}")
    doc_id: str = Field(min_length=1, max_length=32)
    path: str = Field(min_length=1, max_length=1024)
    title: str = Field(default="", max_length=500)
    heading: str = Field(default="", max_length=500, description="所属 Markdown 标题")
    chunk_index: int = Field(ge=0, description="文档内序号（从 0 开始）")
    content: str = Field(min_length=1, description="正文（不含 heading）")
    char_count: int = Field(ge=1, description="正文字符数")


class RagSearchHit(KingPilotBaseModel):
    """一次 BM25 检索命中的分块。"""

    chunk_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str = ""
    heading: str = ""
    chunk_index: int = Field(ge=0)
    content: str = ""
    score: float = Field(ge=0.0)


class KnowledgeSearchResult(KingPilotBaseModel):
    """``kb_search`` 的结构化结果（注入 LLM 与引用校验的统一形状）。"""

    query: str = Field(min_length=1, max_length=200)
    hits: list[RagSearchHit] = Field(default_factory=list)
    hit_count: int = Field(default=0, ge=0)
    no_hit: bool = True
    message: str = ""


class RagSkippedFile(KingPilotBaseModel):
    """一篇被跳过的源文件（只含路径与原因）。"""

    path: str = Field(min_length=1, max_length=1024)
    reason: RagSkipReason


class RagIngestReport(KingPilotBaseModel):
    """一次摄入的结构化结果；单篇失败不影响整库。"""

    added: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    chunks: int = Field(default=0, ge=0)
    written: bool = Field(default=False, description="本次是否真正重写了索引文件")
    skipped: list[RagSkippedFile] = Field(default_factory=list)


class RagIndexPayload(KingPilotBaseModel):
    """``index.json`` 的磁盘结构（读写双向校验）。"""

    schema_version: int = Field(ge=1)
    built_at: datetime
    source_root: str = Field(min_length=1, max_length=2048)
    documents: list[RagDocument] = Field(default_factory=list)
    chunks: list[RagChunk] = Field(default_factory=list)
