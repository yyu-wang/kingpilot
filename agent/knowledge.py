"""``kb_search`` 本地只读工具（M9-T09）。

职责：
- 定义 LLM 侧参数模型（Schema 由 Pydantic 自动生成）
- 把参数交给 ``KnowledgeRetriever``，返回结构化命中

边界：
- 不导入 ``agent.tools``（避免与注册表形成循环导入）；
  ``ToolDefinition`` 的装配在 ``agent/tools.py`` 完成
- 不调用金蝶、不调用 LLM
"""

from pathlib import Path

from pydantic import Field

from config.rag_rules import RAG_ANSWER_TOP_K
from governance.rag.retriever import KnowledgeRetriever
from infrastructure.exceptions import ToolError
from models.base import KingPilotBaseModel
from models.rag import KnowledgeSearchResult

#: 工具名常量：评测集、引用策略、测试统一引用，避免硬编码漂移
KB_SEARCH_TOOL = "kb_search"


class KnowledgeParams(KingPilotBaseModel):
    """``kb_search`` 参数：问题 + 返回条数。"""

    query: str = Field(min_length=1, max_length=200, description="知识问题或关键词")
    limit: int = Field(
        default=RAG_ANSWER_TOP_K,
        ge=1,
        le=10,
        description="返回片段数（1~10，默认 5）",
    )


class KnowledgeService:
    """``kb_search`` 执行器：参数校验 → 检索 → 结构化结果。"""

    def __init__(self, *, index_dir: Path) -> None:
        self._retriever = KnowledgeRetriever(index_dir)

    async def run(self, params: KingPilotBaseModel) -> KnowledgeSearchResult:
        """供 ``ToolDefinition.runner`` 调用。"""
        if not isinstance(params, KnowledgeParams):
            raise ToolError("kb_search 参数模型不匹配")
        return self._retriever.search(params.query, top_k=params.limit)
