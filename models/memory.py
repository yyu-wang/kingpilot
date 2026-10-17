"""三层记忆的值对象（M3-T02，方案 §5.3 支柱九）。

内部值对象用 frozen dataclass（§3.3）：记忆条目一旦写入即不可变，
压缩（T03）/实体合并（T04）都是「产出新对象」而非就地篡改，
审计与回放时历史快照不被后续操作污染。

字段名严格对齐方案 §5.3 数据结构，不发明同义字段：
- Layer 1 RawMessage：最近 N 轮原文 + Tool 结构化返回
- Layer 2 ConversationSummary：旧对话的结构化摘要（含原文范围引用，规范 2）
- Layer 3 EntityRef：全会话持久实体引用（幻觉校验锚点）
- UserProfile：跨会话长期画像（任务卡第三层「长期画像」的载体）
"""

from dataclasses import dataclass, field
from typing import Any, Literal

# Layer 1 原文角色：记忆层不保留 system（系统提示随组装生成，不进历史）
RawRole = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class RawMessage:
    """Layer 1 原始层单条：对话原文 + token 计数 + Tool 结构化结果。

    Why 不存 tool_calls 原文：对话消息由 ``LLMMessage`` 承载；记忆层只保留
    有业务意义的工具返回（``structured_result``），供压缩与答复裁剪引用。
    """

    role: RawRole
    content: str
    token_count: int = 0  # 写入时由 estimate_tokens 自动估算（agent/memory.py）
    structured_result: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntityRef:
    """Layer 3 实体引用：金蝶业务实体的持久锚点（客户/订单/供应商…）。

    ``kingdee_id`` 未知时为空串（如先按名称提及、尚未查编码）；
    去重键见 agent/memory.py 的 upsert 逻辑（id 优先，缺失时退化为类型+名称）。
    """

    entity_type: str  # customer / order / supplier / material...
    display_name: str
    kingdee_id: str = ""  # 金蝶内码（幻觉校验锚点；未知时为空）
    last_mentioned_in_round: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationSummary:
    """Layer 2 摘要层单条：一次压缩的产物（压缩器 M3-T03 产出）。

    规范 2「只摘要不删语义」：``compressed_from_round/to_round`` 是指向
    原文范围的引用，配合 ``key_entities`` 保证压缩后指代信息可回溯。
    """

    summary_text: str  # 任务脉络（1-3 句）
    key_entities: tuple[EntityRef, ...]
    unresolved_questions: tuple[str, ...]
    tool_results_cache: dict[str, Any]  # 只留有业务意义的数字和名称
    compressed_from_round: int
    compressed_to_round: int
    original_token_count: int
    compressed_token_count: int


@dataclass(frozen=True)
class UserProfile:
    """跨会话长期画像（任务卡「长期画像」层）。

    T02 只提供读写形状；画像如何从历史中演化（提取/衰减/冲突合并）归 T07。
    """

    summary_text: str = ""
    facts: dict[str, str] = field(default_factory=dict)  # 如 {"常用客户": "CUS001"}
