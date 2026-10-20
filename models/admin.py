"""管理后台视图模型（订单变更审计 + 测试数据集浏览）。

定位：只服务于管理后台页面，不参与 Agent 主链路。
数据来源：
- 变更记录：``WriteLedger``（before/after 快照 + 字段 diff + 证据链）
- 当前数据：金蝶 ``ExecuteBillQuery``（与业务查询同一协议，不直连沙箱内存库）
"""

from datetime import datetime
from typing import Any

from pydantic import Field

from models.base import KingPilotBaseModel
from models.kingdee import JsonScalar


class FieldChange(KingPilotBaseModel):
    """单个字段的变更（嵌套快照已压平为点号路径）。"""

    field_name: str
    old_value: Any = None
    new_value: Any = None


class ChangeRecordView(KingPilotBaseModel):
    """一条写操作审计记录（管理后台列表/详情共用）。"""

    record_id: str
    timestamp: datetime
    trace_id: str
    form_id: str
    business_key: str
    action: str
    status: str
    user_id: str
    risk_score: float = 0.0
    risk_level: str = ""
    approval_chain: list[str] = Field(default_factory=list)
    user_input: str = ""
    tool_params: dict[str, Any] = Field(default_factory=dict)
    api_response: dict[str, Any] = Field(default_factory=dict)
    field_diff: list[FieldChange] = Field(default_factory=list)
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    record_hash: str = ""


class ChangeListResponse(KingPilotBaseModel):
    """变更记录列表：``total`` 为过滤后的总数，``items`` 已按时间倒序。"""

    total: int
    chain_valid: bool
    items: list[ChangeRecordView] = Field(default_factory=list)


class DatasetResponse(KingPilotBaseModel):
    """一个业务对象的数据集（表头 + 行）。"""

    kind: str
    label: str
    form_id: str
    fields: list[str] = Field(default_factory=list)
    rows: list[dict[str, JsonScalar]] = Field(default_factory=list)
    total: int = 0


class AdminOverview(KingPilotBaseModel):
    """管理后台总览：变更统计 + 各数据集行数。"""

    changes_total: int = 0
    changes_success: int = 0
    changes_failed: int = 0
    by_form: dict[str, int] = Field(default_factory=dict)
    by_action: dict[str, int] = Field(default_factory=dict)
    datasets: dict[str, int] = Field(default_factory=dict)
    chain_valid: bool = True
    generated_at: datetime
