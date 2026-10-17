"""LLM 边界模型（M2-T01）。

方案 §2.3 主循环与 LLM 交互的输入/输出形状。职责边界（防腐层）：
本层只定义「送进 LLM 的上下文」与「LLM 返回的结构化决策」，
厂商 SDK 的原始报文形状（delta/usage 细节等）由 T02 具体 Provider 内部消化。

字段来源对齐：
- ``plan`` 返回 ``PlanResult``（伪代码 ``plan.tool_calls`` 进 Guard 1-5）
- ``reflect`` 返回 ``ReflectionResult``（伪代码 ``continue_execution`` 多步编排）
- ``generate_response`` 返回纯字符串（直接进幻觉校验与 AgentResponse.content）
- ``MemorySnapshot`` 三字段 = 方案 §5.3 支柱九的三层记忆（送进 LLM 的快照）
"""

from typing import Any, Literal

from pydantic import Field

from models.base import KingPilotBaseModel
from models.tool import ToolExecution

# 消息角色闭集：与主流厂商（OpenAI/GLM/Qwen/DeepSeek）function calling 语义对齐
MessageRole = Literal["system", "user", "assistant", "tool"]


class LLMMessage(KingPilotBaseModel):
    """单条对话消息（送进 LLM 的最小单元）。"""

    role: MessageRole = Field(
        description="角色：system 系统提示 / user 用户 / assistant 模型 / tool 工具结果"
    )
    content: str = Field(description="消息正文")
    name: str = Field(default="", description="来源名称（role=tool 时为工具名，其余留空）")
    tool_call_id: str = Field(
        default="", description="工具调用 ID（role=tool 时关联对应 assistant 消息）"
    )


class LLMContext(KingPilotBaseModel):
    """送进 LLM 的完整上下文（伪代码 ``assemble_context_for_llm`` 的产物）。

    ``anchors`` 为防幻觉锚点注入挂载点（方案 §支柱十 Phase 1）：
    实体锚点由主循环预查注入，M3 落地组装逻辑，本层只承载。
    """

    messages: list[LLMMessage] = Field(default_factory=list, description="按时间序的消息列表")
    anchors: list[str] = Field(
        default_factory=list, description="防幻觉实体锚点（如「客户名称已核实: 测试客户001」）"
    )


class ToolSpec(KingPilotBaseModel):
    """工具的 LLM 侧声明（Function Calling 的 schema 部分）。

    Schema 即文档（方案 §4.1 黄金法则）：description 必须写清参数语义、
    取值范围、错误行为。M2-T03 的 ``ToolDefinition`` 组合本模型
    （schema 声明与 Python 实现绑定），本层只承载「LLM 看得见的部分」。
    """

    name: str = Field(min_length=1, description="工具名（LLM 调用时引用，snake_case）")
    description: str = Field(
        min_length=1, description="给 LLM 看的用途说明（何时用/何时不用/错误行为）"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="JSON Schema 对象（参数名/类型/取值范围）"
    )


class ToolCallDecision(KingPilotBaseModel):
    """LLM 决定的一次工具调用（plan 的产出元素）。"""

    tool_name: str = Field(min_length=1, description="目标工具名（须在可见 ToolSpec 列表内）")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="调用参数（进 Guard 2 校验后才执行）"
    )
    rationale: str = Field(default="", description="选择理由（审计与幻觉排查用）")


class PlanResult(KingPilotBaseModel):
    """Phase 1 规划产出（方案 §2.3 伪代码 ``plan.tool_calls``）。"""

    tool_calls: list[ToolCallDecision] = Field(
        default_factory=list, description="待执行的调用序列（空 = 纯聊天无需工具）"
    )
    reasoning: str = Field(default="", description="规划推理过程（审计留痕）")


class ReflectionResult(KingPilotBaseModel):
    """Phase 4 反思产出（Plan-and-Solve 多步编排的续跑决策）。"""

    continue_execution: bool = Field(description="是否继续执行下一批工具调用")
    next_input: str = Field(
        default="", description="继续时的下一轮用户输入（如「查询该客户的订单」）"
    )
    reasoning: str = Field(default="", description="判断依据（审计留痕）")


class ExecutionSummary(KingPilotBaseModel):
    """执行层结果摘要（reflect/generate_response 的输入，伪代码 ``saga_result``）。

    最小诚实形状（D1 决策）：M5 Saga 真类型落地时若形状变化，
    本模型随之演进（协议与 FakeProvider 同步改，改动集中两文件）。
    """

    tool_executions: list[ToolExecution] = Field(
        default_factory=list, description="本轮已执行的工具记录（复用 M0-T06 模型）"
    )
    needs_more_steps: bool = Field(
        default=False, description="结果是否不足以回答用户（触发 reflect 多步编排）"
    )


class MemorySnapshot(KingPilotBaseModel):
    """送进 LLM 的记忆快照（方案 §5.3 支柱九三层记忆的 LLM 视角）。

    Why 字段是「摘要形态」：原始消息与实体图留在主循环侧，LLM 只收
    压缩后的快照——省 token 且天然脱敏（M7 分级字段不出网）。
    """

    layer1_recent: list[str] = Field(default_factory=list, description="工作区层：近期对话逐条文本")
    session_summary: str = Field(default="", description="会话层：本会话摘要（压缩器产出）")
    user_profile: str = Field(default="", description="长期层：用户画像摘要（角色/常用查询偏好）")


class TokenUsage(KingPilotBaseModel):
    """单次 LLM 调用用量（模块规范 4：为 M7 成本核算供数）。"""

    prompt_tokens: int = Field(ge=0, description="输入 token 数")
    completion_tokens: int = Field(ge=0, description="输出 token 数")

    @property
    def total_tokens(self) -> int:
        """总 token 数（trace.llm_token_usage 的 total 来源）。"""
        return self.prompt_tokens + self.completion_tokens


class EntityDraft(KingPilotBaseModel):
    """压缩 LLM 从旧对话中提取的实体草稿（M3-T03 summarize 输出元素）。

    Why 是独立模型而非直接用 models.memory.EntityRef：EntityRef 携带
    ``last_mentioned_in_round`` 等会话记账字段，LLM 不该也无法编造；
    轮次由压缩器用压缩窗口边界填充，边界两侧的职责不混。
    """

    entity_type: str = Field(min_length=1, description="实体类型（customer/order/supplier…）")
    display_name: str = Field(min_length=1, description="实体显示名（原文中的称呼）")
    kingdee_id: str = Field(default="", description="金蝶内码（对话中出现过才填，未知留空）")


class SummaryDraft(KingPilotBaseModel):
    """压缩 LLM 的结构化产出（严格 JSON，方案 §5.3 压缩 Prompt 要点）。

    压缩器据此 + 窗口轮次/token 记账构造 ConversationSummary；
    坏 JSON / 字段不符在 Provider 层归一为 LLMResponseError。
    """

    summary_text: str = Field(min_length=1, description="任务脉络（1-3 句，保留指代对象）")
    key_entities: list[EntityDraft] = Field(
        default_factory=list, description="压缩窗口内提到的关键实体（含金蝶内码）"
    )
    unresolved_questions: list[str] = Field(
        default_factory=list, description="尚未解决的问题（后续轮可能追问）"
    )
    tool_results_cache: dict[str, Any] = Field(
        default_factory=dict,
        description="有业务意义的结果快照（只留数字和名称，不放大段原始报文）",
    )
