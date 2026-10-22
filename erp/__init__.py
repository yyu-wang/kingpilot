"""ERP 适配层（厂商中立契约 + 扩展点）。

本包是 KingPilot 与 ERP 系统对接的统一入口：
- ``types``：中性查询/过滤类型（FilterExpression / BillQuerySpec）
- ``protocol``：ErpClient 客户端契约（@runtime_checkable Protocol + async 上下文）
- ``conditions``：FilterCondition 构造助手
- ``descriptors`` / ``bindings``：端点描述与工具绑定
- ``tool_names``：工具名常量与（对象, 动作）查表
- ``factory``：按 ``settings.erp_vendor`` 构造具体客户端（延迟导入厂商，保持依赖单向）
- ``yonyou/``：用友骨架（实现协议 + 示例端点，未接入真实环境）

厂商实现：
- ``kingdee/``（顶层包，位置不动）：金蝶 K3Cloud 参考适配器
- ``erp/yonyou/``：用友骨架
"""

from erp.conditions import eq, ge, gt, le, like, lt, ne
from erp.protocol import ErpClient
from erp.types import BillQuerySpec, FilterCondition, FilterExpression, FilterOp

__all__ = [
    "BillQuerySpec",
    "ErpClient",
    "FilterCondition",
    "FilterExpression",
    "FilterOp",
    "eq",
    "ge",
    "gt",
    "le",
    "like",
    "lt",
    "ne",
]
