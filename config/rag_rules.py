"""RAG 规则表（M9-T08）。

规则与逻辑分离：本文件只声明常量与目录分级约定，
扫描/分块/索引算法分别在 ``governance/rag/`` 各模块。

分级约定：
- 一级目录名 = 等级名（public / internal / sensitive / high_sensitive）
- 没有等级目录的文档默认 internal
- 本卡只允许 public / internal 入库
"""

from config.data_security import DataLevel

# 支持的文件后缀（大小写不敏感）
RAG_SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".txt"})

# 一级目录名 → 数据分级；未命中默认 internal
RAG_LEVEL_BY_DIR: dict[str, DataLevel] = {
    "public": "public",
    "internal": "internal",
    "sensitive": "sensitive",
    "high_sensitive": "high_sensitive",
}

# 允许进入本地索引的等级（sensitive 及以上整篇拒绝）
RAG_INGEST_ALLOWED_LEVELS: frozenset[str] = frozenset({"public", "internal"})

# ── 分块参数（字符口径，中文按字计）──
RAG_CHUNK_MAX_CHARS = 800
RAG_CHUNK_MIN_CHARS = 60
RAG_CHUNK_OVERLAP_CHARS = 100

# ── 源文件安全上限 ──
RAG_MAX_FILE_BYTES = 1_048_576  # 1 MiB

# ── BM25 参数（业界常用默认值）──
RAG_BM25_K1 = 1.5
RAG_BM25_B = 0.75

# ── 检索参数 ──
RAG_DEFAULT_TOP_K = 5
RAG_MAX_TOP_K = 20

# ── 知识问答（M9-T09）──
RAG_RETRIEVAL_TOP_K = 12  # BM25 候选池
RAG_ANSWER_TOP_K = 5  # 注入上下文的片段数
RAG_MAX_CHUNKS_PER_DOC = 2  # 单文档上限，避免一份手册刷屏
# 相关性下限：BM25 分数 ÷ 查询词数。BM25 总分随问题长度增长，绝对分数不可直接比较；
# 归一化后真命中明显高于假阳性（实测真命中 ≥0.66，无关片段 ≤0.54），
# 低于该值的片段视为无关，整批过滤后走 no_hit，避免把不相关手册当依据引用。
RAG_MIN_AVG_TOKEN_SCORE = 0.55
KNOWLEDGE_NO_HIT_MESSAGE = "知识库没有找到相关文档。"
# 纯聊天/未调用 kb_search 时，模型误用无命中话术的兜底文案
KNOWLEDGE_NOT_SEARCHED_MESSAGE = (
    "本次没有检索知识库。请明确说明要查询的业务数据，或要了解的手册/流程问题。"
)

# ── 索引落盘 ──
RAG_INDEX_FILENAME = "index.json"
RAG_INDEX_SCHEMA_VERSION = 1
