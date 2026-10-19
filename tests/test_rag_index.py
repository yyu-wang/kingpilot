"""M9-T08：分词、BM25 检索与索引持久化测试。"""

import json
from pathlib import Path

import pytest

from governance.rag.index import RagIndex, tokenize, tokenize_query
from infrastructure.exceptions import RagError
from models.rag import RagChunk, RagDocument

_UNIT_HASH = "0" * 64


def _document(doc_id: str, path: str, title: str) -> RagDocument:
    return RagDocument(
        doc_id=doc_id,
        path=path,
        title=title,
        level="public",
        content_hash=_UNIT_HASH,
        size_bytes=100,
        chunk_count=1,
    )


def _chunk(doc_id: str, path: str, title: str, heading: str, content: str) -> RagChunk:
    return RagChunk(
        chunk_id=f"{doc_id}-0000",
        doc_id=doc_id,
        path=path,
        title=title,
        heading=heading,
        chunk_index=0,
        content=content,
        char_count=len(content),
    )


def _corpus() -> tuple[list[RagDocument], list[RagChunk]]:
    specs = [
        (
            "d1",
            "public/sales.md",
            "销售手册",
            "审核销售订单",
            "销售订单保存后需要由主管审核，审核通过后状态变为已审核。",
        ),
        (
            "d2",
            "public/purchase.md",
            "采购手册",
            "采购订单入库",
            "采购订单审核后，仓库按到货数量做采购入库单。",
        ),
        (
            "d3",
            "public/inventory.txt",
            "库存说明",
            "库存说明",
            "库存查询按仓库与物料编码过滤，可用库存等于即时库存减去锁库数量。",
        ),
    ]
    documents = [_document(doc_id, path, title) for doc_id, path, title, _, _ in specs]
    chunks = [_chunk(*spec) for spec in specs]
    return documents, chunks


def test_tokenize_cjk_unigram_and_bigram() -> None:
    tokens = tokenize("销售订单")
    assert {"销", "售", "订", "单"} <= set(tokens)
    assert {"销售", "售订", "订单"} <= set(tokens)


def test_tokenize_latin_casefold_and_digits() -> None:
    tokens = tokenize("SAL_Order 2024")
    assert {"sal", "order", "2024"} <= set(tokens)


def test_tokenize_query_avoids_single_char_noise() -> None:
    terms = tokenize_query("报销流程")
    assert "销" not in terms  # 多字查询不再拆单字，避免「销售」误命中
    assert {"报销", "销流", "流程"} <= set(terms)
    assert tokenize_query("税") == ["税"]


def test_search_hits_expected_chunk() -> None:
    documents, chunks = _corpus()
    hits = RagIndex(documents, chunks).search("审核销售订单")
    assert hits
    assert hits[0].path == "public/sales.md"
    assert hits[0].heading == "审核销售订单"


def test_search_no_match_returns_empty() -> None:
    documents, chunks = _corpus()
    assert RagIndex(documents, chunks).search("报销流程") == []


def test_search_single_char_query_still_works() -> None:
    documents, chunks = _corpus()
    hits = RagIndex(documents, chunks).search("库")
    assert any(hit.path == "public/inventory.txt" for hit in hits)


def test_search_empty_query_returns_empty() -> None:
    documents, chunks = _corpus()
    assert RagIndex(documents, chunks).search("   ") == []


def test_search_top_k_is_bounded() -> None:
    documents, chunks = _corpus()
    index = RagIndex(documents, chunks)
    assert len(index.search("审核", top_k=1)) == 1
    assert len(index.search("审核", top_k=999)) == 2  # 只有两篇含「审核」
    assert len(index.search("审核", top_k=0)) == 1  # 下界抬到 1


def test_search_ties_sorted_by_chunk_id() -> None:
    documents = [_document("db", "public/b.md", "B"), _document("da", "public/a.md", "A")]
    chunks = [
        _chunk("db", "public/b.md", "B", "规则", "相同内容"),
        _chunk("da", "public/a.md", "A", "规则", "相同内容"),
    ]
    hits = RagIndex(documents, chunks).search("相同内容")
    assert [hit.chunk_id for hit in hits] == ["da-0000", "db-0000"]


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    documents, chunks = _corpus()
    index_path = tmp_path / "rag_index" / "index.json"
    RagIndex(documents, chunks, source_root=str(tmp_path)).save(index_path)

    loaded = RagIndex.load(index_path)
    assert len(loaded.documents) == 3
    assert len(loaded.chunks) == 3
    assert [hit.chunk_id for hit in loaded.search("审核销售订单")] == [
        hit.chunk_id for hit in RagIndex(documents, chunks).search("审核销售订单")
    ]
    assert index_path.stat().st_mode & 0o777 == 0o600


def test_load_missing_index_returns_empty(tmp_path: Path) -> None:
    index = RagIndex.load(tmp_path / "missing.json")
    assert index.documents == ()
    assert index.search("任意") == []


def test_load_corrupted_index_raises(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(RagError) as exc:
        RagIndex.load(index_path)
    assert exc.value.code == "rag_index_invalid"


def test_load_schema_mismatch_raises(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "built_at": "2026-10-19T00:00:00Z",
                "source_root": "/tmp/knowledge",
                "documents": [],
                "chunks": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RagError) as exc:
        RagIndex.load(index_path)
    assert exc.value.code == "rag_index_invalid"


def test_load_chunk_without_document_raises(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "built_at": "2026-10-19T00:00:00Z",
                "source_root": "/tmp/knowledge",
                "documents": [],
                "chunks": [
                    _chunk("ghost", "public/x.md", "X", "标题", "内容").model_dump(mode="json")
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RagError) as exc:
        RagIndex.load(index_path)
    assert exc.value.code == "rag_index_invalid"
