"""三层记忆的读写门面（M3-T02，方案 §5.3 支柱九）。

职责边界：
- ``ConversationMemory``：会话内容器（Layer 1 原文 / Layer 2 摘要 / Layer 3 实体图），
  可变状态持有者（值对象本身 frozen，容器负责有序追加与去重合并）
- ``MemoryStore``：门面，= 一个会话的三层 + 跨会话长期画像（UserProfile）+
  ``to_snapshot()`` 产出 M2 的 LLM 视图

本任务只做读写接口：压缩阈值与触发归 T03（接口已备好：``add_summary``/
``evict_raw``），实体抽取归 T04（``upsert_entity`` 已支持按内码去重合并），
主循环接入与持久化分别归 T03/T07。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from models.llm import LLMMessage, MemorySnapshot
from models.memory import (
    ConversationSummary,
    EntityRef,
    RawMessage,
    RawRole,
    UserProfile,
)

# 快照文本里结构化结果的截断长度：视图层裁剪，原始结果仍完整留在 Layer 1
_STRUCTURED_PREVIEW_LIMIT = 200


def render_raw_for_llm(messages: Sequence[RawMessage]) -> list[LLMMessage]:
    """Layer 1 原文 → LLM 消息（压缩器与主循环 Phase 0 共用同一映射）。

    Why tool→user：role=tool 的消息必须带匹配的 tool_call_id，否则被
    OpenAI 兼容 API 拒绝；记忆回放/压缩场景只是把「工具查到的事实」喂给
    模型，user 角色承载事实文本最稳，加「工具结果:」前缀保持角色语义可读。
    """
    rendered: list[LLMMessage] = []
    for message in messages:
        if message.role == "tool":
            payload = message.content or json.dumps(message.structured_result, ensure_ascii=False)
            rendered.append(LLMMessage(role="user", content=f"工具结果: {payload}"))
        else:
            rendered.append(LLMMessage(role=message.role, content=message.content))
    return rendered


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 字符按 1 token、其余按 4 字符/token 向上取整。

    INFERRED 启发式（非精确分词）：Why 不用 tiktoken——避免为非 OpenAI 模型
    （DeepSeek/千问）引入其分词器造成「精确的错误」，压缩阈值（T03）只需
    量级正确。M7 成本核算要精确值时，本函数是唯一替换点（provider 回执的
    真实 usage 也已在记录）。
    """
    cjk_count = sum(1 for ch in text if _is_cjk(ch))
    other_count = len(text) - cjk_count
    return cjk_count + (other_count + 3) // 4


def _is_cjk(char: str) -> bool:
    """是否按 CJK 单字计 token（含 CJK 统一表意/中文标点/全角符号区）。"""
    code = ord(char)
    return 0x4E00 <= code <= 0x9FFF or 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF


@dataclass
class ConversationMemory:
    """一个会话的三层容器（方案 §5.3 ConversationMemory，去掉预算字段）。

    token_budget/阈值不放在本类：预算是压缩器（T03）的策略参数，进 Settings；
    容器只忠实持有数据并暴露计量只读属性（used_tokens/raw_rounds）供策略判断。
    """

    session_id: str
    raw_buffer: list[RawMessage] = field(default_factory=list)
    summary_buffer: list[ConversationSummary] = field(default_factory=list)
    entity_graph: list[EntityRef] = field(default_factory=list)

    @property
    def used_tokens(self) -> int:
        """Layer 1 当前 token 合计（T03 紧急/积极压缩的判断输入）。"""
        return sum(message.token_count for message in self.raw_buffer)

    @property
    def raw_rounds(self) -> int:
        """对话轮数：按 user 消息计数（assistant/tool 跟随消息不另计轮）。"""
        return sum(1 for message in self.raw_buffer if message.role == "user")

    def upsert_entity(self, ref: EntityRef) -> None:
        """实体按业务键去重合并：同键更新提及轮次与属性，不同键追加。

        去重键 = (entity_type, kingdee_id)；内码未知（空串）时退化为
        (entity_type, display_name)——先按名称提及、后补到内码时视为新实体，
        由 T04 抽取层负责把「名称占位」合并进「带内码」实体。
        """
        for index, existing in enumerate(self.entity_graph):
            if self._entity_key(existing) == self._entity_key(ref):
                self.entity_graph[index] = EntityRef(
                    entity_type=existing.entity_type,
                    display_name=existing.display_name,
                    kingdee_id=existing.kingdee_id,
                    last_mentioned_in_round=max(
                        existing.last_mentioned_in_round, ref.last_mentioned_in_round
                    ),
                    attributes={**existing.attributes, **ref.attributes},
                )
                return
        self.entity_graph.append(ref)

    @staticmethod
    def _entity_key(ref: EntityRef) -> tuple[str, str]:
        """内码优先、缺失时退化名称的去重键（见 upsert_entity）。"""
        return (ref.entity_type, ref.kingdee_id or ref.display_name)

    def evict_raw_before_round(self, round_threshold: int) -> None:
        """清除所属轮次 < ``round_threshold`` 的原文（T03 压缩后调用）。

        消息所属轮次 = 其之前（含自身）最近一条 user 消息的累计轮号；
        不往 RawMessage 上加轮次字段（§5.3 数据结构不发明字段），轮次从
        user 消息边界现场推导。
        """
        kept: list[RawMessage] = []
        round_no = 0
        for message in self.raw_buffer:
            if message.role == "user":
                round_no += 1
            if round_no >= round_threshold:
                kept.append(message)
        self.raw_buffer = kept


class MemoryStore:
    """记忆读写门面：会话三层 + 跨会话长期画像 + LLM 视图产出。"""

    def __init__(self, session_id: str, profile: UserProfile | None = None) -> None:
        self._memory = ConversationMemory(session_id=session_id)
        self._profile = profile or UserProfile()

    @property
    def session_id(self) -> str:
        """会话 ID（多轮聚合键，与 AgentTrace.session_id 对齐）。"""
        return self._memory.session_id

    @property
    def used_tokens(self) -> int:
        """Layer 1 token 合计（压缩策略的直通只读属性）。"""
        return self._memory.used_tokens

    @property
    def raw_rounds(self) -> int:
        """当前对话轮数。"""
        return self._memory.raw_rounds

    # ── 只读访问（M3-T03 压缩器取窗口/记账用）──

    def raw_messages(self) -> tuple[RawMessage, ...]:
        """Layer 1 原文只读视图（压缩窗口切分用，不暴露可变 list）。"""
        return tuple(self._memory.raw_buffer)

    def summaries(self) -> tuple[ConversationSummary, ...]:
        """Layer 2 摘要只读视图（压缩器推算绝对轮次与 L2 占用用）。"""
        return tuple(self._memory.summary_buffer)

    # ── Layer 1 读写 ──

    def add_raw(
        self,
        role: RawRole,
        content: str,
        structured_result: dict[str, Any] | None = None,
    ) -> RawMessage:
        """追加一条 Layer 1 原文，token_count 写入时自动估算。"""
        message = RawMessage(
            role=role,
            content=content,
            token_count=estimate_tokens(content),
            structured_result=structured_result or {},
        )
        self._memory.raw_buffer.append(message)
        return message

    def replace_last_assistant(self, content: str) -> None:
        """替换最近一条 assistant 原文（M7-T02 DLP 阻断后净化记忆）。

        主循环先写回记忆、后做出域扫描；命中阻断时必须把已落进 Layer 1
        的原始答复同步替换，否则敏感内容会随记忆再次进入后续 LLM 上下文。
        """
        for index in range(len(self._memory.raw_buffer) - 1, -1, -1):
            message = self._memory.raw_buffer[index]
            if message.role == "assistant":
                self._memory.raw_buffer[index] = replace(
                    message,
                    content=content,
                    token_count=estimate_tokens(content),
                )
                return

    # ── Layer 2 读写 ──

    def add_summary(self, summary: ConversationSummary) -> None:
        """挂载一条压缩摘要（T03 压缩器产出后调用）。"""
        self._memory.summary_buffer.append(summary)

    def evict_raw_before_round(self, round_threshold: int) -> None:
        """压缩后清除旧原文（委托容器，保留门面单一入口）。"""
        self._memory.evict_raw_before_round(round_threshold)

    # ── Layer 3 读写 ──

    def upsert_entity(self, ref: EntityRef) -> None:
        """实体去重合并入图（T04 抽取后逐实体调用）。"""
        self._memory.upsert_entity(ref)

    def entities(self, entity_type: str | None = None) -> tuple[EntityRef, ...]:
        """查询实体；``entity_type`` 给定时按类型过滤。"""
        if entity_type is None:
            return tuple(self._memory.entity_graph)
        return tuple(ref for ref in self._memory.entity_graph if ref.entity_type == entity_type)

    def find_entity(
        self,
        entity_type: str,
        *,
        kingdee_id: str | None = None,
        display_name: str | None = None,
    ) -> EntityRef | None:
        """按类型 + 内码或名称精确查找单个实体（回答「之前提到的客户」用）。

        ``kingdee_id`` 优先于 ``display_name``；都不给时返回 None。
        """
        for ref in self._memory.entity_graph:
            if ref.entity_type != entity_type:
                continue
            if kingdee_id is not None and ref.kingdee_id == kingdee_id:
                return ref
            if kingdee_id is None and display_name is not None and ref.display_name == display_name:
                return ref
        return None

    def entities_mentioned_after(self, round_no: int) -> tuple[EntityRef, ...]:
        """``last_mentioned_in_round > round_no`` 的实体（支持「最近提到的」语义）。"""
        return tuple(
            ref for ref in self._memory.entity_graph if ref.last_mentioned_in_round > round_no
        )

    def entity_anchors(self) -> list[str]:
        """实体图渲染为防幻觉锚点文本（Phase 0 注入 LLMContext.anchors）。

        格式 ``{type}:{name}({id})``；无内码则 ``{type}:{name}``。
        「之前提到的客户」可回答性的来源：LLM 通过 anchors 看到会话全部实体。
        """
        anchors: list[str] = []
        for ref in self._memory.entity_graph:
            if ref.kingdee_id:
                anchors.append(f"{ref.entity_type}:{ref.display_name}({ref.kingdee_id})")
            else:
                anchors.append(f"{ref.entity_type}:{ref.display_name}")
        return anchors

    # ── 长期画像读写 ──

    def update_profile(self, profile: UserProfile) -> None:
        """整体替换长期画像（frozen 值对象，演化逻辑归 T07）。"""
        self._profile = profile

    def get_profile(self) -> UserProfile:
        """读取当前长期画像。"""
        return self._profile

    # ── LLM 视图 ──

    def to_snapshot(self) -> MemorySnapshot:
        """产出送 LLM 的三层快照（M2 MemorySnapshot 契约）。

        拼装：Layer 1 逐条文本（tool 消息附结构化结果预览）→ Layer 2 摘要
        时序拼接 → 画像直取。实体图（Layer 3）经 LLMContext.anchors 注入，
        不在本快照字段内（M2 已预留锚点通道）。
        """
        return MemorySnapshot(
            layer1_recent=[self._render_raw(message) for message in self._memory.raw_buffer],
            session_summary="\n".join(s.summary_text for s in self._memory.summary_buffer),
            user_profile=self._profile.summary_text,
        )

    @staticmethod
    def _render_raw(message: RawMessage) -> str:
        """Layer 1 单条渲染为文本；tool 空文本时用结构化结果预览补足。"""
        if message.content:
            return f"{message.role}: {message.content}"
        if message.structured_result:
            preview = json.dumps(message.structured_result, ensure_ascii=False)
            return f"tool: {preview[:_STRUCTURED_PREVIEW_LIMIT]}"
        return f"{message.role}: "
