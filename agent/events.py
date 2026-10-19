"""Agent 运行事件（M9-T02）：SSE 流式输出的唯一事件契约。

安全边界：
- 事件模型**没有**工具参数/结果/错误原文字段，从类型层杜绝数据出域
- 最终答复走 ``final`` 事件，且必须由 API 层在 DLP 后发送
"""

from collections.abc import Callable
from typing import Literal

from pydantic import Field

from models.base import KingPilotBaseModel

EventType = Literal["phase", "tool", "final", "error", "done"]
EventStatus = Literal["start", "success", "error"]


class AgentEvent(KingPilotBaseModel):
    """一条可序列化的运行事件。

    Why 单模型而非多个子类：事件字段少、量大；单模型让 SSE 序列化与
    客户端解析只有一条路径，新增可选字段不会破坏旧客户端。
    """

    event: EventType = Field(description="事件类型")
    phase: str = Field(default="", description="阶段名：plan/guard/execute/reflect/generate")
    status: EventStatus | None = Field(default=None, description="阶段/工具状态")
    tool_name: str = Field(default="", description="工具名（仅 tool 事件）")
    latency_ms: float = Field(default=0.0, description="工具耗时（仅 tool 结束事件）")
    content: str = Field(default="", description="最终答复（仅 final 事件）")
    trace_id: str = Field(default="", description="Trace ID（final 事件）")
    session_id: str = Field(default="", description="会话 ID（final/done 事件）")
    blocked: bool = Field(default=False, description="是否被 DLP/护栏拦截（final）")
    cancelled: bool = Field(default=False, description="是否被 HITL 取消（final）")
    hallucination_warning: bool = Field(default=False, description="幻觉告警（final）")
    approval_pending: bool = Field(default=False, description="是否进入审批待处理（final）")
    approval_id: str = Field(default="", description="审批单 ID（final）")
    code: str = Field(default="", description="错误码（error 事件）")
    message: str = Field(default="", description="安全错误文案（error 事件）")


# 同步、旁路回调：Core 用 queue.put_nowait 注入，不引入额外 await 点
AgentEventSink = Callable[[AgentEvent], None]
