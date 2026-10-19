"""Badcase 处理台策略（M9-T07）。

规则与逻辑分离：本文件只声明准入角色、长度上限与数据集命名规则，
文件存储与导出逻辑在 ``governance/badcase.py``。
"""

# 评测集是发布门禁输入：只允许管理与运营角色标记/导出
BADCASE_REVIEWER_ROLES: frozenset[str] = frozenset({"admin", "manager"})

MAX_BADCASE_QUESTION_CHARS = 1000
MAX_BADCASE_REASON_CHARS = 500
MAX_EXPECTED_BEHAVIOR_CHARS = 1000
MAX_BADCASE_CATEGORY_CHARS = 64

DEFAULT_DATASET_VERSION = "v1"
DATASET_FILENAME = "tool_selection.json"

# 版本号会作为目录名：只允许字母数字开头，防路径穿越
DATASET_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"
