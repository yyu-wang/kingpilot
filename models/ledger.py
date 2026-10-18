"""写操作账本模型（M5-T03，方案 §5.5 支柱十七）。

``WriteRecord`` 是落库边界模型（Pydantic 校验 + JSON 序列化），包含：
- 账本一：before/after 快照 + 字段级 diff（回答“改了什么”）
- 账本二：``ProvenanceChain`` 证据链（回答“为什么改、谁授权”）
- 防篡改：``prev_record_hash`` / ``record_hash``（链式，M5-T04）
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from models.base import KingPilotBaseModel

WriteAction = Literal["save", "edit", "submit", "audit", "unaudit", "delete"]
WriteStatus = Literal["success", "failed"]


class FieldDiff(KingPilotBaseModel):
    """单个字段的变更（嵌套 dict 用点号路径，如 ``FCustomerID.FNumber``）。"""

    field_name: str
    old_value: Any = None
    new_value: Any = None


class ProvenanceChain(KingPilotBaseModel):
    """完整证据链：从用户一句话到数据落地的每一步（T05 负责构建/报告）。"""

    user_original_input: str = ""
    user_id: str = ""
    session_id: str = ""
    llm_reasoning: str = ""
    tool_call_params: dict[str, Any] = Field(default_factory=dict)
    guard_check_results: list[dict[str, Any]] = Field(default_factory=list)
    risk_score: float = 0.0
    risk_level: str = ""
    confirmation_record: dict[str, Any] | None = None
    approval_chain: list[str] = Field(default_factory=list)
    api_request: dict[str, Any] = Field(default_factory=dict)
    api_response: dict[str, Any] = Field(default_factory=dict)


class WriteRecord(KingPilotBaseModel):
    """每次写操作落一条，append-only 不可修改。"""

    record_id: str
    trace_id: str
    saga_id: str = ""
    timestamp: datetime
    form_id: str
    business_key: str
    kingdee_doc_id: str = ""
    action: WriteAction
    status: WriteStatus = "success"

    # ── 账本一：状态快照 ──
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    field_diff: list[FieldDiff] = Field(default_factory=list)

    # ── 账本二：证据链 ──
    provenance: ProvenanceChain = Field(default_factory=ProvenanceChain)

    # ── 防篡改 ──
    prev_record_hash: str
    record_hash: str

    def to_hash_payload(self) -> dict[str, Any]:
        """hash 计算输入（JSON 模式，保证落库前后一致）。"""
        return self.model_dump(mode="json")
