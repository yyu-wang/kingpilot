"""死信模型（M6-T06）。

一段无法自动补偿的写事务会进入死信队列，等待人工核对/重放。
``saga_snapshot`` 保留完整事务现场，便于人工判断残留数据。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from models.base import KingPilotBaseModel

DeadLetterStatus = Literal["pending", "resolved"]


class DeadLetter(KingPilotBaseModel):
    """一条待人工处理的失败事务。"""

    dl_id: str
    saga_id: str
    trace_id: str = ""
    created_at: datetime
    status: DeadLetterStatus = "pending"
    original_error: str = ""
    compensation_error: str = ""
    manual_action: str = ""
    request: dict[str, Any] = Field(default_factory=dict)
    saga_snapshot: dict[str, Any] = Field(default_factory=dict)
    replay_attempts: int = 0
    last_replay_error: str = ""
    resolved_at: datetime | None = None
    resolution_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转 JSON 可序列化的字典。"""
        return self.model_dump(mode="json")
