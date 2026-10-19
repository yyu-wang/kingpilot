"""知识引用策略（M9-T09）。

核心原则：
1. **引用必须可回溯**：格式固定为 ``《文档标题》第 N 段（段落标题）``
2. **无命中不编造**：只要 ``kb_search`` 返回 no_hit，最终答复由代码覆盖
3. **LLM 不合规就兜底**：生成结果没有合法引用时，用 top1 原文做确定性摘录

本模块不调用 LLM，纯函数便于单测与审计。
"""

from collections.abc import Sequence

from pydantic import Field

from config.rag_rules import KNOWLEDGE_NO_HIT_MESSAGE
from models.base import KingPilotBaseModel
from models.rag import KnowledgeSearchResult, RagSearchHit


class CitationResult(KingPilotBaseModel):
    """引用校验后的最终答复与标记。"""

    content: str
    citations: list[str] = Field(default_factory=list)
    no_hit: bool = False
    fallback_used: bool = False
    enforced: bool = True


def format_citation(hit: RagSearchHit) -> str:
    """``《文档标题》第 N 段``；N = 文档内分块序号 + 1。"""
    return f"《{hit.title}》第 {hit.chunk_index + 1} 段"


def format_citation_with_heading(hit: RagSearchHit) -> str:
    """带段落标题的完整引用前缀，便于用户定位原文。"""
    heading = f"（{hit.heading}）" if hit.heading else ""
    return f"根据{format_citation(hit)}{heading}"


def render_references(hits: Sequence[RagSearchHit]) -> str:
    """确定性参考来源列表（去重，保持命中顺序）。"""
    lines = ["参考来源："]
    seen: set[str] = set()
    for hit in hits:
        label = f"{format_citation(hit)}（{hit.heading}）" if hit.heading else format_citation(hit)
        if label in seen:
            continue
        seen.add(label)
        lines.append(f"- {label}")
    return "\n".join(lines)


def enforce_citations(answer: str, result: KnowledgeSearchResult) -> CitationResult:
    """强制引用：无命中固定文案；引用缺失/伪造用原文摘录兜底。"""
    if result.no_hit or not result.hits:
        return CitationResult(content=KNOWLEDGE_NO_HIT_MESSAGE, no_hit=True)

    citations = _unique([format_citation(hit) for hit in result.hits])
    if not any(citation in answer for citation in citations):
        top = result.hits[0]
        content = f"{format_citation_with_heading(top)}：{top.content}"
        return CitationResult(
            content=f"{content}\n\n{render_references(result.hits)}",
            citations=citations,
            fallback_used=True,
        )

    content = answer.strip()
    if "参考来源" not in content:
        content = f"{content}\n\n{render_references(result.hits)}"
    return CitationResult(content=content, citations=citations)


def render_knowledge_fallback(result: KnowledgeSearchResult) -> str:
    """LLM 不可用时，把知识命中渲染成带引用的确定性文本。"""
    if result.no_hit or not result.hits:
        return KNOWLEDGE_NO_HIT_MESSAGE
    parts: list[str] = []
    for hit in result.hits:
        parts.append(f"{format_citation_with_heading(hit)}：{hit.content}")
    return "\n\n".join(parts)


def _unique(values: Sequence[str]) -> list[str]:
    """保持顺序去重。"""
    return list(dict.fromkeys(values))
