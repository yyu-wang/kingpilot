"""M7-T01 · 数据分级与脱敏规则表（方案 §5.4 支柱十一）。

运行时只读。字段分级与脱敏策略集中在此，逻辑在 ``safety/datasecurity.py``。
默认口径：
- public / internal：原样输出
- sensitive：部分掩码（手机/身份证/邮箱等）
- high_sensitive：``[REDACTED]`` 拒出
"""

from typing import Literal

DataLevel = Literal["public", "internal", "sensitive", "high_sensitive"]
MaskStrategy = Literal["none", "partial", "hash", "drop"]

LEVEL_STRATEGY: dict[DataLevel, MaskStrategy] = {
    "public": "none",
    "internal": "none",
    "sensitive": "partial",
    "high_sensitive": "drop",
}

# 字段名（支持关联字段的末段匹配，如 FCustomerID.FPhone）
FIELD_CLASSIFICATION: dict[str, DataLevel] = {
    "FPhone": "sensitive",
    "FMobile": "sensitive",
    "FEmail": "sensitive",
    "FContact": "sensitive",
    "FAddress": "sensitive",
    "FIDCard": "high_sensitive",
    "FBankAccount": "high_sensitive",
    "FSalary": "high_sensitive",
}
