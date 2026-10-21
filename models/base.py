"""基础类型与枚举（M0-T06）。

所有模型统一继承 ``BaseModel`` 并设置 ``extra="forbid"``（fail-closed），
防止拼写错误字段静默通过（呼应 M0-T04 配置中心原则）。
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Phase(StrEnum):
    """主循环阶段（方案 §2.3）。"""

    MEMORY = "memory"  # Phase 0: 记忆管理
    DOMAIN_FILTER = "domain_filter"  # Phase 0.5: 数据域隔离
    PLANNING = "planning"  # Phase 1: 规划层
    SAFETY = "safety"  # Phase 2: 安全护栏
    EXECUTION = "execution"  # Phase 3: 执行层
    REFLECTION = "reflection"  # Phase 4: 反思 + 结果生成
    HALLUCINATION_CHECK = "hallucination_check"  # Phase 5: 幻觉校验
    OBSERVABILITY = "observability"  # Phase 6: 记忆更新 + 可观测性


class RiskLevel(StrEnum):
    """风险等级（方案 §17 支柱一）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SagaStatus(StrEnum):
    """Saga 事务状态（方案 §17 支柱三）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    ROLLED_BACK = "rolled_back"


# ── 类型别名（提高可读性）──

# 用户角色（M4-T08 扩展为数据域矩阵需要的细粒度角色；保留既有三值兼容）
Role = Literal[
    "admin",
    "manager",
    "operator",
    "viewer",
    "salesperson",
    "purchaser",
    "warehouse",
    "finance_staff",
]

#: 角色中文展示名（仅用于界面展示，鉴权始终以英文 role 值为准）
ROLE_LABELS: dict[str, str] = {
    "admin": "管理员",
    "manager": "经理",
    "operator": "操作员",
    "viewer": "只读成员",
    "salesperson": "销售人员",
    "purchaser": "采购人员",
    "warehouse": "仓储人员",
    "finance_staff": "财务人员",
}


FormId = str  # 金蝶业务对象标识（如 "SAL_Order"）
BusinessKey = str  # 业务单据号（如 "SO20241015001"）


class KingPilotBaseModel(BaseModel):
    """项目基础模型（统一配置）。"""

    model_config = ConfigDict(
        extra="forbid",  # 未知字段拒绝（fail-closed）
        validate_assignment=True,  # 赋值时也校验（防止运行期绕过）
        arbitrary_types_allowed=False,  # 禁止任意类型（强制 Pydantic 可序列化）
    )
