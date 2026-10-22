"""用友适配器骨架（演示「新增厂商三步」的第 1、2 步）。

本包是**可运行的骨架**，不是可用实现：
- 第 1 步：``client.py`` 实现 ``ErpClient`` 三方法（查询/写动作/释放）；
- 第 2 步：``endpoints.py`` 声明该厂商的端点描述符（字段集与可过滤字段）；
- 第 3 步：``erp/factory.py`` 注册 ``yonyou`` 分支。

未接入真实环境时，``query`` / ``invoke_action`` 一律 fail-closed 抛
``YonyouNotConfiguredError``——不返回空结果伪装成功。

真实接入还需补三块（与金蝶适配器一一对应）：
- 认证：用友 OpenAPI 的令牌换取（对应 ``kingdee/auth.py``）
- 查询翻译：``BillQuerySpec`` → 用友查询参数（对应 ``kingdee/filter_string.py``）
- 写动作映射：动作名 → 用友服务方法（对应 ``KingdeeClient.invoke_action``）
"""

from erp.yonyou.client import YonyouClient, YonyouNotConfiguredError
from erp.yonyou.endpoints import PURCHASE_ORDER

__all__ = ["PURCHASE_ORDER", "YonyouClient", "YonyouNotConfiguredError"]
