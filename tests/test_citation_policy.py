"""M9-T09：引用策略单测。"""

from config.rag_rules import KNOWLEDGE_NO_HIT_MESSAGE
from governance.rag.answer import (
    enforce_citations,
    format_citation,
    render_knowledge_fallback,
    render_references,
)
from models.rag import KnowledgeSearchResult, RagSearchHit


def _hit(
    *,
    chunk_id: str = "d1-0001",
    title: str = "销售订单操作手册",
    heading: str = "审核销售订单",
    chunk_index: int = 1,
    content: str = "销售订单保存后由主管审核。",
) -> RagSearchHit:
    return RagSearchHit(
        chunk_id=chunk_id,
        doc_id="d1",
        path="public/sales.md",
        title=title,
        heading=heading,
        chunk_index=chunk_index,
        content=content,
        score=1.0,
    )


def _result(hits: list[RagSearchHit]) -> KnowledgeSearchResult:
    return KnowledgeSearchResult(
        query="销售订单怎么审核？",
        hits=hits,
        hit_count=len(hits),
        no_hit=not hits,
    )


def test_fake_citation_falls_back_to_real_chunk() -> None:
    citation = enforce_citations("根据《不存在的文档》第 1 段，随便说说。", _result([_hit()]))

    assert citation.fallback_used is True
    assert citation.no_hit is False
    assert "《不存在的文档》" not in citation.content
    assert citation.content.startswith("根据《销售订单操作手册》第 2 段（审核销售订单）：")


def test_legit_citation_is_kept_and_references_appended() -> None:
    answer = "根据《销售订单操作手册》第 2 段（审核销售订单），保存后由主管审核。"
    citation = enforce_citations(answer, _result([_hit()]))

    assert citation.fallback_used is False
    assert citation.content.startswith(answer)
    assert "参考来源：" in citation.content
    assert "《销售订单操作手册》第 2 段" in citation.content
    assert citation.citations == ["《销售订单操作手册》第 2 段"]


def test_missing_citation_uses_extractive_fallback() -> None:
    citation = enforce_citations("审核就是点一下按钮。", _result([_hit()]))

    assert citation.fallback_used is True
    assert "审核就是点一下按钮" not in citation.content
    assert "销售订单保存后由主管审核。" in citation.content
    assert "参考来源：" in citation.content


def test_no_hit_is_forced_message() -> None:
    citation = enforce_citations("公司规定差旅报销可以全额报销。", _result([]))

    assert citation.content == KNOWLEDGE_NO_HIT_MESSAGE
    assert citation.no_hit is True
    assert citation.citations == []


def test_references_are_deduplicated() -> None:
    first = _hit(chunk_id="d1-0001")
    second = _hit(chunk_id="d1-0002")

    references = render_references([first, second])

    assert references.count(f"- {format_citation(first)}（审核销售订单）") == 1
    assert len(references.splitlines()) == 2  # 标题 + 一条去重后的引用


def test_knowledge_fallback_renders_citations() -> None:
    content = render_knowledge_fallback(_result([_hit()]))
    assert content.startswith("根据《销售订单操作手册》第 2 段（审核销售订单）：")

    empty = render_knowledge_fallback(_result([]))
    assert empty == KNOWLEDGE_NO_HIT_MESSAGE
