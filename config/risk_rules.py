"""M4-T07 · 风险规则表（方案 §5.1 支柱一）。

运行时**只读**。规则只声明参数，评分逻辑在 ``safety/risk_engine.py``——
保持「规则与逻辑分离」，调整阈值/权重只改本文件。

八条规则与方案原值一一对应：
    写操作基础分 30 / 金额每万 +1（上限 30）/ 审核反审核 25 / 删除 40 /
    大批量每 10 条 +1（上限 20）/ 敏感对象 20 / 用户画像 ×0.5 / 非工作时间 10×0.3
"""

from dataclasses import dataclass
from datetime import time
from typing import Literal

RiskRuleKind = Literal[
    "write_base",
    "amount",
    "action",
    "batch",
    "sensitive",
    "user_risk",
    "off_hours",
]

# HITL 四档（与方案 §5.1 分级路由一致）
ApprovalLevel = Literal["auto", "confirm", "dual_approve", "read_only"]

#: 四档中文展示名（仅用于界面/摘要展示，路由判断始终以英文 level 为准）
RISK_LEVEL_LABELS: dict[str, str] = {
    "auto": "自动执行",
    "confirm": "单票确认",
    "dual_approve": "双人复核",
    "read_only": "只读",
}


@dataclass(frozen=True)
class RiskRule:
    """单条评分规则（声明式参数，由 RiskEngine 按 kind 分发计算）。"""

    rule_id: str
    label: str
    kind: RiskRuleKind
    points: float = 0.0  # 固定分
    per_unit_points: float = 0.0  # 比例分（每 unit 加多少分）
    unit: float = 1.0
    max_points: float = 100.0  # 单规则原始分上限
    weight: float = 1.0  # 加权系数
    actions: tuple[str, ...] = ()  # kind=action 时匹配的动作

    def __post_init__(self) -> None:
        if self.unit <= 0:
            raise ValueError(f"规则 {self.rule_id} 的 unit 必须大于 0")
        if self.weight < 0:
            raise ValueError(f"规则 {self.rule_id} 的 weight 不能为负")


@dataclass(frozen=True)
class RiskTier:
    """风险分档：``score <= max_score`` 即落入该档。"""

    level: ApprovalLevel
    max_score: float


# 敏感业务对象（命中 +20）
SENSITIVE_OBJECTS: dict[str, str] = {
    "GL_Voucher": "会计凭证",
    "AP_Bill": "应付单",
    "AR_Bill": "应收单",
    "STK_Loss": "库存盘点",
}

RISK_RULES: tuple[RiskRule, ...] = (
    RiskRule(rule_id="write_base", label="写操作基础分", kind="write_base", points=30),
    RiskRule(
        rule_id="amount",
        label="金额阈值",
        kind="amount",
        per_unit_points=1,
        unit=10_000,
        max_points=30,
    ),
    RiskRule(
        rule_id="audit",
        label="审核/反审核",
        kind="action",
        points=25,
        actions=("audit", "unaudit"),
    ),
    RiskRule(
        rule_id="delete",
        label="删除操作",
        kind="action",
        points=40,
        actions=("delete",),
    ),
    RiskRule(
        rule_id="batch",
        label="大批量操作",
        kind="batch",
        per_unit_points=1,
        unit=10,
        max_points=20,
    ),
    RiskRule(
        rule_id="sensitive_object",
        label="敏感业务对象",
        kind="sensitive",
        points=20,
    ),
    RiskRule(
        rule_id="user_risk",
        label="用户历史风险画像",
        kind="user_risk",
        weight=0.5,
    ),
    RiskRule(
        rule_id="off_hours",
        label="非工作时间",
        kind="off_hours",
        points=10,
        weight=0.3,
    ),
)

# 四档路由阈值（含上限）；顺序由 RiskEngine 按 max_score 排序
RISK_TIERS: tuple[RiskTier, ...] = (
    RiskTier(level="auto", max_score=20),
    RiskTier(level="confirm", max_score=50),
    RiskTier(level="dual_approve", max_score=80),
    RiskTier(level="read_only", max_score=100),
)

# 工作时间判定参数（周一~周五 09:00-18:00，左闭右开）
BUSINESS_HOURS_START = time(9, 0)
BUSINESS_HOURS_END = time(18, 0)
BUSINESS_WEEKDAYS = frozenset({0, 1, 2, 3, 4})
