"""M9-T09：知识检索服务与 kb_search 本地工具测试。"""

import shutil
import time
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

from agent.knowledge import KB_SEARCH_TOOL, KnowledgeService
from agent.tools import build_default_registry
from config.rag_rules import KNOWLEDGE_NO_HIT_MESSAGE
from governance.rag.ingest import RagIngestor
from governance.rag.retriever import KnowledgeRetriever
from infrastructure.exceptions import ToolError
from kingdee.client import KingdeeClient
from models.rag import KnowledgeSearchResult
from models.user import UserContext
from safety.permission_engine import PermissionEngine
from safety.risk_engine import build_risk_context

_FIXTURES = Path(__file__).parent / "fixtures" / "rag"


def _build_index(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "knowledge"
    shutil.copytree(_FIXTURES, source)
    index_dir = tmp_path / "rag_index"
    RagIngestor(source_root=source, index_dir=index_dir).refresh()
    return index_dir, source


def test_search_returns_expected_chunk(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)

    result = KnowledgeRetriever(index_dir).search("审核销售订单")

    assert result.no_hit is False
    assert result.hit_count > 0
    assert result.hits[0].path == "public/sales-order-guide.md"
    assert result.hits[0].title == "销售订单操作手册"
    assert result.hits[0].heading == "审核销售订单"


def test_new_order_question_hits_creation_steps(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)

    result = KnowledgeRetriever(index_dir).search("新建销售订单需要哪些步骤？")

    assert result.no_hit is False
    assert result.hits[0].path == "public/sales-order-guide.md"
    assert result.hits[0].heading == "创建销售订单"
    assert "新建销售订单" in result.hits[0].content
    assert "保存" in result.hits[0].content


def test_delivery_question_hits_delivery_section(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)

    result = KnowledgeRetriever(index_dir).search("销售订单怎么发货？")

    assert result.no_hit is False
    assert result.hits[0].path == "public/sales-order-guide.md"
    assert result.hits[0].heading == "销售订单发货"
    assert "发货通知单" in result.hits[0].content


def test_rerank_prefers_heading_match(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)

    result = KnowledgeRetriever(index_dir).search("供应商对账")

    assert result.hits
    assert result.hits[0].path == "internal/purchase-order-guide.md"
    assert result.hits[0].heading == "供应商对账"


def test_no_match_is_no_hit(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)

    result = KnowledgeRetriever(index_dir).search("差旅报销制度")

    assert result.no_hit is True
    assert result.message == KNOWLEDGE_NO_HIT_MESSAGE
    assert result.hits == []


def test_missing_index_is_no_hit(tmp_path: Path) -> None:
    result = KnowledgeRetriever(tmp_path / "missing").search("任意问题")

    assert result.no_hit is True
    assert result.message == KNOWLEDGE_NO_HIT_MESSAGE


def test_corrupt_index_is_no_hit(tmp_path: Path) -> None:
    index_dir = tmp_path / "rag_index"
    index_dir.mkdir()
    (index_dir / "index.json").write_text("{broken", encoding="utf-8")

    result = KnowledgeRetriever(index_dir).search("任意问题")

    assert result.no_hit is True


def test_per_document_cap(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)

    result = KnowledgeRetriever(index_dir).search("销售订单", top_k=10)

    counts = Counter(hit.doc_id for hit in result.hits)
    assert counts
    assert max(counts.values()) <= 2


def test_index_reloads_after_ingest(tmp_path: Path) -> None:
    index_dir, source = _build_index(tmp_path)
    retriever = KnowledgeRetriever(index_dir)
    assert retriever.search("差旅报销").no_hit is True

    time.sleep(0.01)  # 保证 mtime_ns 变化
    (source / "public" / "travel.md").write_text(
        "# 差旅制度\n\n## 报销\n\n差旅报销需要提供发票。", encoding="utf-8"
    )
    RagIngestor(source_root=source, index_dir=index_dir).refresh()

    result = retriever.search("差旅报销")
    assert result.no_hit is False
    assert result.hits[0].path == "public/travel.md"


async def test_local_tool_executes(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)
    tool = build_default_registry(knowledge=KnowledgeService(index_dir=index_dir)).get(
        KB_SEARCH_TOOL
    )

    result = await tool.execute(
        cast(KingdeeClient, object()), {"query": "审核销售订单", "limit": 3}
    )

    assert tool.is_local is True
    assert isinstance(result, KnowledgeSearchResult)
    assert result.no_hit is False
    assert result.hit_count <= 3


async def test_local_tool_rejects_invalid_limit(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)
    tool = build_default_registry(knowledge=KnowledgeService(index_dir=index_dir)).get(
        KB_SEARCH_TOOL
    )

    with pytest.raises(ToolError, match="参数校验失败"):
        await tool.execute(cast(KingdeeClient, object()), {"query": "审核", "limit": 11})


def test_viewer_can_see_and_use_local_tool(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)
    registry = build_default_registry(knowledge=KnowledgeService(index_dir=index_dir))
    engine = PermissionEngine()
    user = UserContext(user_id="viewer1", role="viewer")

    visible = [spec.name for spec in engine.domain_filter(user, registry.all_tools())]
    decision = engine.check(user, registry.get(KB_SEARCH_TOOL), {"query": "审核"})

    assert KB_SEARCH_TOOL in visible
    assert decision.allowed is True
    assert decision.domain is None


def test_deny_tools_hides_local_tool(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)
    registry = build_default_registry(knowledge=KnowledgeService(index_dir=index_dir))
    engine = PermissionEngine()
    user = UserContext(user_id="viewer1", role="viewer", deny_tools=[KB_SEARCH_TOOL])

    visible = [spec.name for spec in engine.domain_filter(user, registry.all_tools())]
    decision = engine.check(user, registry.get(KB_SEARCH_TOOL), {"query": "审核"})

    assert KB_SEARCH_TOOL not in visible
    assert decision.allowed is False


def test_risk_context_handles_missing_endpoint(tmp_path: Path) -> None:
    index_dir, _ = _build_index(tmp_path)
    tool = build_default_registry(knowledge=KnowledgeService(index_dir=index_dir)).get(
        KB_SEARCH_TOOL
    )

    context = build_risk_context(
        tool, {"query": "审核", "limit": 5}, UserContext(user_id="u", role="viewer")
    )

    assert context.form_id == ""
    assert context.tool_type == "query"
