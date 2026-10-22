"""电商平台 RAG 基础测试（语料：``tests/fixtures/rag_ecommerce``）。

覆盖五层：
1. 摄入：分级门禁、DLP、幂等、增量更新、审计脱敏
2. 检索：主题命中、同义问法、单文档限流、无命中
3. 引用：合法引用保留、伪造引用兜底、无命中固定话术
4. Agent 集成：只调 kb_search、引用强制、拒绝编造
5. 边界：索引缺失/损坏降级为 no_hit

全部使用 FakeProvider 与本地索引，不依赖真实 LLM / 金蝶。
"""

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

from agent.core import AgentCore
from agent.knowledge import KB_SEARCH_TOOL, KnowledgeService
from agent.tools import ToolRegistry, build_default_registry
from config.rag_rules import KNOWLEDGE_NO_HIT_MESSAGE
from config.settings import Settings
from governance.rag.answer import enforce_citations, format_citation
from governance.rag.ingest import RagIngestor
from governance.rag.retriever import KnowledgeRetriever
from kingdee.client import KingdeeClient
from models.llm import PlanResult, ReflectionResult, ToolCallDecision
from models.rag import KnowledgeSearchResult, RagSearchHit
from observability.audit import AuditLogger, JsonlAuditSink
from tests.fakes import FakeProvider

_FIXTURES = Path(__file__).parent / "fixtures" / "rag_ecommerce"

_ALLOWED_PATHS = {
    "public/product-guide.md",
    "public/order-guide.md",
    "public/payment-guide.md",
    "public/inventory-guide.md",
    "public/logistics-guide.md",
    "public/after-sales-guide.md",
    "public/promotion-guide.md",
    "public/member-guide.md",
    "internal/customer-service-sop.md",
}

_ECOMMERCE_CASES = [
    ("商品怎么上架？", "public/product-guide.md", "商品上架"),
    ("订单可以修改收货地址吗？", "public/order-guide.md", "修改收货地址"),
    ("支付超时订单会怎样？", "public/payment-guide.md", "支付超时"),
    ("可售库存怎么计算？", "public/inventory-guide.md", "可售库存"),
    ("快递签收后发现破损怎么办？", "public/logistics-guide.md", "签收与拒收"),
    ("怎么申请退货退款？", "public/after-sales-guide.md", "退货退款流程"),
    ("优惠券可以叠加使用吗？", "public/promotion-guide.md", "优惠叠加"),
    ("会员积分怎么获得？", "public/member-guide.md", "积分获取"),
    ("客户工单多久响应？", "internal/customer-service-sop.md", "工单分级"),
]


def _build(tmp_path: Path) -> tuple[Path, Path]:
    """复制独立电商语料并构建索引，返回（源目录, 索引目录）。"""
    source = tmp_path / "knowledge"
    shutil.copytree(_FIXTURES, source)
    index_dir = tmp_path / "rag_index"
    RagIngestor(source_root=source, index_dir=index_dir).refresh()
    return source, index_dir


def _search(index_dir: Path, query: str) -> KnowledgeSearchResult:
    return KnowledgeRetriever(index_dir).search(query)


def _registry(index_dir: Path) -> ToolRegistry:
    return build_default_registry(knowledge=KnowledgeService(index_dir=index_dir))


def _top_hit(index_dir: Path, query: str) -> RagSearchHit:
    result = _search(index_dir, query)
    assert result.no_hit is False
    return result.hits[0]


def _core(registry: ToolRegistry, answer: str, query: str) -> AgentCore:
    provider = FakeProvider(
        plan_queue=[
            PlanResult(
                tool_calls=[
                    ToolCallDecision(
                        tool_name=KB_SEARCH_TOOL,
                        arguments={"query": query, "limit": 5},
                    )
                ]
            )
        ],
        reflection_queue=[ReflectionResult(continue_execution=False)],
        response_queue=[answer],
    )
    return AgentCore(
        provider,
        registry,
        cast(KingdeeClient, object()),
        Settings(_env_file=None),
    )


# ────────────────────────── 1. 摄入层 ──────────────────────────


def test_ecommerce_corpus_ingests_allowed_documents(tmp_path: Path) -> None:
    source = tmp_path / "knowledge"
    shutil.copytree(_FIXTURES, source)
    ingestor = RagIngestor(source_root=source, index_dir=tmp_path / "rag_index")

    report = ingestor.refresh()

    assert report.added == 9
    assert report.chunks > 0
    assert report.written is True
    assert {document.path for document in ingestor.load_index().documents} == _ALLOWED_PATHS


def test_sensitive_document_is_never_indexed(tmp_path: Path) -> None:
    source, index_dir = _build(tmp_path)

    report = RagIngestor(source_root=source, index_dir=index_dir).refresh()
    raw = (index_dir / "index.json").read_text(encoding="utf-8")

    assert [(item.path, item.reason) for item in report.skipped] == [
        ("sensitive/customer-contacts.md", "level_denied")
    ]
    assert "13800138000" not in raw
    assert "sensitive/customer-contacts.md" not in raw


def test_repeat_ingest_is_idempotent(tmp_path: Path) -> None:
    source, index_dir = _build(tmp_path)
    ingestor = RagIngestor(source_root=source, index_dir=index_dir)
    before = (index_dir / "index.json").read_text(encoding="utf-8")

    report = ingestor.refresh()

    assert report.unchanged == 9
    assert report.written is False
    assert (index_dir / "index.json").read_text(encoding="utf-8") == before


def test_public_pii_document_is_rejected(tmp_path: Path) -> None:
    source, index_dir = _build(tmp_path)
    (source / "public" / "leak.md").write_text("# 联系人\n\n手机号 13900139000。", encoding="utf-8")

    report = RagIngestor(source_root=source, index_dir=index_dir).refresh()

    assert any(
        item.path == "public/leak.md" and item.reason == "sensitive_content"
        for item in report.skipped
    )
    assert "13900139000" not in (index_dir / "index.json").read_text(encoding="utf-8")


def test_document_update_reindexes(tmp_path: Path) -> None:
    source, index_dir = _build(tmp_path)
    path = source / "public" / "product-guide.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## 预售定金\n\n预售商品需要先支付定金。\n",
        encoding="utf-8",
    )

    report = RagIngestor(source_root=source, index_dir=index_dir).refresh()

    assert report.updated == 1
    assert report.unchanged == 8
    assert _search(index_dir, "预售定金").hits[0].path == "public/product-guide.md"


def test_audit_event_has_no_document_content(tmp_path: Path) -> None:
    source, _ = _build(tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    ingestor = RagIngestor(
        source_root=source,
        index_dir=tmp_path / "rag_index_audit",
        audit=AuditLogger(JsonlAuditSink(audit_path)),
    )

    ingestor.refresh()
    line = audit_path.read_text(encoding="utf-8").strip()
    event = json.loads(line)

    assert event["event_type"] == "rag.indexed"
    assert event["detail"]["added"] == 9
    assert "商品" not in line
    assert "退货" not in line


# ────────────────────────── 2. 检索层 ──────────────────────────


@pytest.mark.parametrize(("question", "path", "heading"), _ECOMMERCE_CASES)
def test_ecommerce_question_hits_expected_section(
    tmp_path: Path, question: str, path: str, heading: str
) -> None:
    _, index_dir = _build(tmp_path)

    result = _search(index_dir, question)

    assert result.no_hit is False
    assert result.hits[0].path == path
    assert result.hits[0].heading == heading
    assert result.hits[0].content


@pytest.mark.parametrize(
    ("question", "path", "heading"),
    [
        ("快递一直不更新怎么办？", "public/logistics-guide.md", "物流轨迹"),
        ("退款多久到账？", "public/after-sales-guide.md", "售后时效"),
    ],
)
def test_synonym_questions_hit_expected_section(
    tmp_path: Path, question: str, path: str, heading: str
) -> None:
    _, index_dir = _build(tmp_path)

    result = _search(index_dir, question)

    assert result.no_hit is False
    assert result.hits[0].path == path
    assert result.hits[0].heading == heading


@pytest.mark.parametrize(
    "question",
    [
        "差旅报销标准是什么？",
        "年假有几天？",
        # 语料里只出现零散「库存/订单」字词、但没有对应章节：必须按无命中处理，
        # 不能把《可售库存》《取消订单》当作依据引用
        "库存盘点的操作步骤是什么？",
        "采购订单入库流程是什么？",
    ],
)
def test_out_of_corpus_question_is_no_hit(tmp_path: Path, question: str) -> None:
    _, index_dir = _build(tmp_path)

    result = _search(index_dir, question)

    assert result.no_hit is True
    assert result.hits == []
    assert result.message == KNOWLEDGE_NO_HIT_MESSAGE


def test_per_document_cap(tmp_path: Path) -> None:
    _, index_dir = _build(tmp_path)

    result = _search(index_dir, "订单")
    counts = Counter(hit.doc_id for hit in result.hits)

    assert counts
    assert max(counts.values()) <= 2


# ────────────────────────── 3. 引用层 ──────────────────────────


def test_legit_citation_is_kept(tmp_path: Path) -> None:
    _, index_dir = _build(tmp_path)
    query = "怎么申请退货退款？"
    result = _search(index_dir, query)
    top = result.hits[0]
    answer = f"根据{format_citation(top)}（{top.heading}），签收后 7 天内可以申请无理由退货。"

    citation = enforce_citations(answer, result)

    assert citation.fallback_used is False
    assert citation.content.startswith(answer)
    assert "参考来源：" in citation.content
    assert format_citation(top) in citation.citations


def test_fake_citation_falls_back_to_real_chunk(tmp_path: Path) -> None:
    _, index_dir = _build(tmp_path)
    result = _search(index_dir, "怎么申请退货退款？")
    top = result.hits[0]

    citation = enforce_citations("根据《不存在的手册》第 1 段，随时可以全额退款。", result)

    assert citation.fallback_used is True
    assert "《不存在的手册》" not in citation.content
    assert citation.content.startswith(
        f"根据《{top.title}》第 {top.chunk_index + 1} 段（{top.heading}）"
    )


def test_no_hit_forces_fixed_message(tmp_path: Path) -> None:
    _, index_dir = _build(tmp_path)
    result = _search(index_dir, "差旅报销标准是什么？")

    citation = enforce_citations("差旅报销可以全额报销。", result)

    assert citation.no_hit is True
    assert citation.content == KNOWLEDGE_NO_HIT_MESSAGE
    assert citation.citations == []


# ────────────────────────── 4. Agent 集成 ──────────────────────────


async def test_knowledge_question_uses_kb_search_only(tmp_path: Path) -> None:
    _, index_dir = _build(tmp_path)
    query = "怎么申请退货退款？"
    top = _top_hit(index_dir, query)
    answer = f"根据{format_citation(top)}（{top.heading}），签收后 7 天内可以申请无理由退货。"
    core = _core(_registry(index_dir), answer, query)

    response = await core.run(query)

    assert response.trace is not None
    assert [item.tool_name for item in response.trace.tool_executions] == [KB_SEARCH_TOOL]
    assert format_citation(top) in response.trace.citations
    assert response.trace.citation_enforced is True
    assert response.trace.citation_fallback is False
    assert "参考来源：" in response.content


async def test_noncompliant_answer_is_replaced(tmp_path: Path) -> None:
    _, index_dir = _build(tmp_path)
    query = "怎么申请退货退款？"
    top = _top_hit(index_dir, query)
    core = _core(_registry(index_dir), "退货随时都可以，不需要审核。", query)

    response = await core.run(query)

    assert response.trace is not None
    assert response.trace.citation_fallback is True
    assert "不需要审核" not in response.content
    assert response.content.startswith(f"根据《{top.title}》第 {top.chunk_index + 1} 段")


async def test_no_hit_overrides_llm_hallucination(tmp_path: Path) -> None:
    empty_index = tmp_path / "empty_index"
    query = "差旅报销标准是什么？"
    core = _core(_registry(empty_index), "差旅报销可以全额报销。", query)

    response = await core.run(query)

    assert response.trace is not None
    assert response.content == KNOWLEDGE_NO_HIT_MESSAGE
    assert response.trace.citation_no_hit is True
    assert response.trace.citations == []


# ────────────────────────── 5. 边界 ──────────────────────────


def test_missing_index_is_no_hit(tmp_path: Path) -> None:
    result = KnowledgeRetriever(tmp_path / "missing").search("商品怎么上架？")

    assert result.no_hit is True
    assert result.message == KNOWLEDGE_NO_HIT_MESSAGE


def test_corrupt_index_is_no_hit(tmp_path: Path) -> None:
    index_dir = tmp_path / "rag_index"
    index_dir.mkdir()
    (index_dir / "index.json").write_text("{broken", encoding="utf-8")

    result = KnowledgeRetriever(index_dir).search("商品怎么上架？")

    assert result.no_hit is True
