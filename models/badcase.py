"""Badcase 记录模型（M9-T07）。

只保留「问题 + 期望工具 + 分类 + 原因/期望行为」这类可进入评测集的字段；
原始 Trace、工具结果与最终答复一律不落 Badcase，避免评测集成为 PII 扩散面。
"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from models.base import KingPilotBaseModel

BadcaseStatus = Literal["marked", "exported"]


class BadcaseRecord(KingPilotBaseModel):
    """一条人工标记的 Badcase（一单一个 JSON 文件）。"""

    badcase_id: str = Field(default_factory=lambda: uuid4().hex)
    trace_id: str
    question: str = Field(max_length=1000)
    expected_tool: str | None = None
    expected_behavior: str = Field(default="", max_length=1000)
    category: str = Field(default="badcase", max_length=64)
    reason: str = Field(max_length=500)
    status: BadcaseStatus = "marked"
    created_by: str
    created_at: datetime
    exported_dataset: str = ""
    exported_question_id: str = ""
    exported_at: datetime | None = None
