"""公共导出（M0-T06）。

外部模块只需 ``from models import UserContext, AgentResponse, ...``。
"""

from models.agent import AgentResponse, AgentTrace
from models.base import BusinessKey, FormId, KingPilotBaseModel, Phase, RiskLevel, Role, SagaStatus
from models.kingdee import (
    BillQueryParams,
    BillQueryResult,
    DocumentStatus,
    LoginResult,
    ResponseStatus,
    extract_response_status,
    rows_to_dicts,
)
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    MessageRole,
    PlanResult,
    ReflectionResult,
    TokenUsage,
    ToolCallDecision,
    ToolSpec,
)
from models.tool import ToolExecution
from models.user import UserContext

__all__ = [
    "AgentResponse",
    # Agent
    "AgentTrace",
    # 金蝶协议（M1-T02、M1-T03、M1-T04）
    "BillQueryParams",
    "BillQueryResult",
    "BusinessKey",
    "DocumentStatus",
    "ExecutionSummary",
    "FormId",
    # 基础
    "KingPilotBaseModel",
    # LLM 边界（M2-T01）
    "LLMContext",
    "LLMMessage",
    "LoginResult",
    "MemorySnapshot",
    "MessageRole",
    "Phase",
    "PlanResult",
    "ReflectionResult",
    "ResponseStatus",
    "RiskLevel",
    "Role",
    "SagaStatus",
    "TokenUsage",
    # 工具
    "ToolCallDecision",
    "ToolExecution",
    "ToolSpec",
    # 用户
    "UserContext",
    "extract_response_status",
    "rows_to_dicts",
]
