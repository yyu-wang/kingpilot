"""审批中心策略（M9-T04）。

规则与逻辑分离：本文件只声明准入角色与票数，状态机在 governance/approval.py。
"""

# 实际可投审批票的角色（required_approvers 仅作业务提示，不参与准入）
APPROVER_ROLES: frozenset[str] = frozenset({"admin", "manager"})

# 双人复核：两票且必须来自两个不同 user_id
DEFAULT_REQUIRED_APPROVALS = 2

# 审批意见长度上限
MAX_APPROVAL_COMMENT_CHARS = 500
