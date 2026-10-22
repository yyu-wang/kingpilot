"""工具执行记录（M0-T06）。

方案 §2.3 主循环中 Phase 3（执行层）每次工具调用的完整记录。
幂等键预留（M3 Saga 事务用）。
"""

from typing import Any

from pydantic import Field

from models.base import KingPilotBaseModel


class ToolExecution(KingPilotBaseModel):
    """单次工具执行记录。"""

    tool_name: str = Field(description="工具名称（如 'erp_query_sales_order'）")
    parameters: dict[str, Any] = Field(description="调用参数（脱敏后）")
    result: Any = Field(default=None, description="执行结果（成功时）")
    error: str | None = Field(default=None, description="错误信息（失败时）")
    idempotency_key: str = Field(default="", description="幂等键（M3 Saga 预留）")
    latency_ms: float = Field(default=0.0, description="执行耗时（毫秒）")
