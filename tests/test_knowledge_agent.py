"""M9-T09：知识问答主循环集成测试（FakeProvider + 真索引）。"""

import shutil
from pathlib import Path
from typing import cast

from agent.core import AgentCore
from agent.knowledge import KB_SEARCH_TOOL, KnowledgeService
from agent.memory import MemoryStore
from agent.tools import ToolRegistry, build_default_registry
from config.rag_rules import KNOWLEDGE_NO_HIT_MESSAGE
from config.settings import Settings
from governance.rag.ingest import RagIngestor
from kingdee.client import KingdeeClient
from models.llm import PlanResult, ReflectionResult, ToolCallDecision
from tests.fakes import FakeProvider

_FIXTURES = Path(__file__).parent / "fixtures" / "rag"


def _registry(tmp_path: Path) -> ToolRegistry:
    source = tmp_path / "knowledge"
    shutil.copytree(_FIXTURES, source)
    index_dir = tmp_path / "rag_index"
    RagIngestor(source_root=source, index_dir=index_dir).refresh()
    return build_default_registry(knowledge=KnowledgeService(index_dir=index_dir))


def _plan(query: str) -> PlanResult:
    return PlanResult(
        tool_calls=[
            ToolCallDecision(
                tool_name=KB_SEARCH_TOOL,
                arguments={"query": query, "limit": 5},
            )
        ]
    )


def _core(
    registry: ToolRegistry,
    answer: str,
    query: str,
    *,
    memory: MemoryStore | None = None,
) -> AgentCore:
    provider = FakeProvider(
        plan_queue=[_plan(query)],
        reflection_queue=[ReflectionResult(continue_execution=False)],
        response_queue=[answer],
    )
    return AgentCore(
        provider,
        registry,
        cast(KingdeeClient, object()),
        Settings(_env_file=None),
        memory=memory,
    )


async def test_knowledge_question_uses_kb_search_only(tmp_path: Path) -> None:
    core = _core(
        _registry(tmp_path),
        "根据《销售订单操作手册》第 2 段（审核销售订单），保存后由主管审核。",
        "销售订单怎么审核？",
    )

    response = await core.run("销售订单怎么审核？")

    assert response.trace is not None
    assert [item.tool_name for item in response.trace.tool_executions] == [KB_SEARCH_TOOL]
    assert "《销售订单操作手册》第 2 段" in response.trace.citations
    assert response.trace.citation_enforced is True
    assert response.trace.citation_fallback is False
    assert response.trace.hallucination_detected is False
    assert "参考来源：" in response.content


async def test_noncompliant_answer_is_replaced(tmp_path: Path) -> None:
    core = _core(_registry(tmp_path), "审核就是点一下按钮。", "销售订单怎么审核？")

    response = await core.run("销售订单怎么审核？")

    assert response.trace is not None
    assert response.trace.citation_fallback is True
    assert response.content.startswith("根据《销售订单操作手册》第 2 段")
    assert "点一下按钮" not in response.content
    assert response.trace.citations


async def test_no_hit_overrides_llm_hallucination(tmp_path: Path) -> None:
    empty_registry = build_default_registry(
        knowledge=KnowledgeService(index_dir=tmp_path / "empty_index")
    )
    core = _core(
        empty_registry,
        "差旅报销可以全额报销，不需要发票。",
        "差旅报销流程是什么？",
    )

    response = await core.run("差旅报销流程是什么？")

    assert response.trace is not None
    assert response.content == KNOWLEDGE_NO_HIT_MESSAGE
    assert response.trace.citation_no_hit is True
    assert response.trace.citations == []


async def test_memory_stores_enforced_answer(tmp_path: Path) -> None:
    memory = MemoryStore(session_id="kb-session")
    core = _core(
        _registry(tmp_path),
        "审核就是点一下按钮。",
        "销售订单怎么审核？",
        memory=memory,
    )

    response = await core.run("销售订单怎么审核？")

    assert memory.raw_messages()[-1].role == "assistant"
    assert memory.raw_messages()[-1].content == response.content
    assert response.content.startswith("根据《销售订单操作手册》")
