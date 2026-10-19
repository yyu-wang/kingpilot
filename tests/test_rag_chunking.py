"""M9-T08：Markdown 分块器测试。"""

from config.rag_rules import RAG_CHUNK_MAX_CHARS, RAG_CHUNK_OVERLAP_CHARS
from governance.rag.chunking import chunk_document, extract_title
from models.rag import RagChunk


def _chunk(text: str) -> list[RagChunk]:
    return chunk_document(text, doc_id="doc0000000000001", path="public/a.md", title="默认标题")


def test_heading_sections_keep_heading() -> None:
    text = "# 手册\n\n## 创建订单\n\n先选客户再保存。\n\n## 审核订单\n\n由主管审核。"
    chunks = _chunk(text)
    assert [chunk.heading for chunk in chunks] == ["创建订单", "审核订单"]
    assert chunks[0].content == "先选客户再保存。"
    assert chunks[1].content == "由主管审核。"


def test_short_paragraphs_are_packed() -> None:
    text = "## 标题\n\n第一段内容。\n\n第二段内容。"
    chunks = _chunk(text)
    assert len(chunks) == 1
    assert "第一段内容。" in chunks[0].content
    assert "第二段内容。" in chunks[0].content
    assert chunks[0].char_count <= RAG_CHUNK_MAX_CHARS


def test_long_paragraph_splits_with_overlap() -> None:
    paragraph = "".join(str(index % 10) for index in range(1500))
    chunks = _chunk(f"## 长段落\n\n{paragraph}")
    assert len(chunks) == 3
    assert all(chunk.char_count <= RAG_CHUNK_MAX_CHARS for chunk in chunks)
    overlap = paragraph[RAG_CHUNK_MAX_CHARS - RAG_CHUNK_OVERLAP_CHARS : RAG_CHUNK_MAX_CHARS]
    assert chunks[1].content.startswith(overlap)


def test_short_document_is_kept() -> None:
    chunks = _chunk("短文档内容")
    assert len(chunks) == 1
    assert chunks[0].content == "短文档内容"
    assert chunks[0].heading == "默认标题"


def test_text_without_heading_uses_title() -> None:
    chunks = _chunk("库存查询按仓库过滤。\n\n盘点差异走调整单。")
    assert chunks[0].heading == "默认标题"


def test_heading_without_body_is_skipped() -> None:
    chunks = _chunk("# 一级标题\n\n## 二级标题\n\n正文内容。")
    assert len(chunks) == 1
    assert chunks[0].heading == "二级标题"


def test_chunk_ids_are_stable() -> None:
    text = "# 手册\n\n## 创建订单\n\n先选客户再保存。"
    first = _chunk(text)
    second = _chunk(text)
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert first[0].chunk_id.endswith("-0000")


def test_extract_title() -> None:
    assert extract_title("# 销售手册\n\n正文", fallback="a") == "销售手册"
    assert extract_title("没有标题", fallback="a") == "a"
