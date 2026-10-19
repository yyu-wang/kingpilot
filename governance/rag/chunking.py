"""Markdown 知识文档分块（M9-T08）。

分块策略（由粗到细）：
1. 按 Markdown 标题（``#``～``######``）切 section，section 标题即引用标题
2. section 内按空行切段落，贪心打包到 ``RAG_CHUNK_MAX_CHARS``
3. 单段落超长 → 按 ``MAX - OVERLAP`` 步长硬切，保留重叠避免答案被切断
4. 尾部碎片优先并入上一块；整篇过短时仍保留一块（不丢文档）

设计约束：
- ``chunk_id`` 只依赖文档内序号，内容不变时保持稳定（T09 引用可回溯）
- 不解析表格/代码块结构，按普通段落处理（避免引入 Markdown 解析依赖）
"""

import re

from config.rag_rules import (
    RAG_CHUNK_MAX_CHARS,
    RAG_CHUNK_MIN_CHARS,
    RAG_CHUNK_OVERLAP_CHARS,
)
from models.rag import RagChunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_PARAGRAPH_JOINER = "\n\n"


def extract_title(text: str, *, fallback: str) -> str:
    """取首个 H1 作为文档标题；没有 H1 时回退到文件名 stem。"""
    match = _TITLE_RE.search(text)
    return match.group(1).strip() if match is not None else fallback


def chunk_document(text: str, *, doc_id: str, path: str, title: str) -> list[RagChunk]:
    """把一篇文档切成可检索的 chunk 列表；空文档返回空列表。"""
    chunks: list[RagChunk] = []
    index = 0
    for heading, body in _split_sections(text, default_heading=title):
        for content in _pack_paragraphs(body):
            chunks.append(
                RagChunk(
                    chunk_id=f"{doc_id}-{index:04d}",
                    doc_id=doc_id,
                    path=path,
                    title=title,
                    heading=heading,
                    chunk_index=index,
                    content=content,
                    char_count=len(content),
                )
            )
            index += 1
    return chunks


def _split_sections(text: str, *, default_heading: str) -> list[tuple[str, str]]:
    """按标题切分；首个标题前的内容归入默认标题。"""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [(default_heading, body)] if body else []

    sections: list[tuple[str, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append((default_heading, preamble))

    for position, match in enumerate(matches):
        heading = match.group(2).strip() or default_heading
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:  # 只有标题没有正文的 section 不产出空 chunk
            sections.append((heading, body))
    return sections


def _pack_paragraphs(body: str) -> list[str]:
    """段落贪心打包 + 超长硬切 + 尾部短块合并。"""
    paragraphs = [part.strip() for part in _PARAGRAPH_SPLIT_RE.split(body) if part.strip()]
    pieces: list[str] = []
    buffer = ""

    for paragraph in paragraphs:
        if len(paragraph) > RAG_CHUNK_MAX_CHARS:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(_hard_split(paragraph))
            continue

        if not buffer:
            buffer = paragraph
        elif len(buffer) + len(_PARAGRAPH_JOINER) + len(paragraph) <= RAG_CHUNK_MAX_CHARS:
            buffer = f"{buffer}{_PARAGRAPH_JOINER}{paragraph}"
        else:
            pieces.append(buffer)
            buffer = paragraph

    if buffer:
        pieces.append(buffer)
    return _merge_short_tail(pieces)


def _hard_split(paragraph: str) -> list[str]:
    """超长段落按固定步长硬切，相邻块保留 ``RAG_CHUNK_OVERLAP_CHARS`` 重叠。"""
    step = RAG_CHUNK_MAX_CHARS - RAG_CHUNK_OVERLAP_CHARS
    if step <= 0:  # 防御：配置被改坏时退化为不重叠，避免死循环
        step = RAG_CHUNK_MAX_CHARS
    return [
        piece
        for start in range(0, len(paragraph), step)
        if (piece := paragraph[start : start + RAG_CHUNK_MAX_CHARS]).strip()
    ]


def _merge_short_tail(pieces: list[str]) -> list[str]:
    """尾部碎片优先并入上一块；允许合并后略超上限，但不超过上限 + 下限。"""
    if len(pieces) < 2 or len(pieces[-1]) >= RAG_CHUNK_MIN_CHARS:
        return pieces
    merged = f"{pieces[-2]}{_PARAGRAPH_JOINER}{pieces[-1]}"
    if len(merged) <= RAG_CHUNK_MAX_CHARS + RAG_CHUNK_MIN_CHARS:
        pieces[-2:] = [merged]
    return pieces
