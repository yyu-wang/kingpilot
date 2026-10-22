"""厂商中立 ERP 客户端契约。

Why Protocol 而非 ABC：结构化子类型（structural subtyping）让
``KingdeeClient`` 无需显式继承即可满足契约（避免循环导入与单继承约束）；
``@runtime_checkable`` 让 ``isinstance`` 检查可用（demo 脚本与 skeleton
测试都依赖这一点）。

Why 返回值用 ``models.kingdee.BillQueryResult`` / ``ActionResult`` 而非
另起中性结果模型：这两个模型的形状（``form_id/field_keys/rows`` 与
``success/bill_no/message``）已足够通用，且现有工具层、安全层、审计层
都已基于它们构建。另起中性模型会传染 25+ 文件，收益不抵成本。
若未来厂商需要扩展字段（如 SAP 的 ``__metadata``），再中性化不迟。
"""

from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from erp.types import BillQuerySpec
from models.kingdee import ActionResult, BillQueryResult

__all__ = ["ErpClient"]


@runtime_checkable
class ErpClient(Protocol):
    """厂商中立 ERP 客户端契约。

    实现者需提供三方法：查询、写动作、资源释放；并支持 ``async with``
    （入口装配统一用上下文管理释放连接池，避免各入口各写一套 try/finally）。
    """

    async def query(self, spec: BillQuerySpec) -> BillQueryResult:
        """执行单据查询（幂等，允许自动重试）。

        Args:
            spec: 中性查询请求。``spec.filter`` 的字段白名单与值类型已由
                Guard 2 校验；``spec.limit`` 硬上限由 Guard 4 负责，本层不越权。

        Returns:
            类型化结果（含按键转换后的行数据），不返回裸二维数组。
        """
        ...

    async def invoke_action(
        self,
        action: str,
        form_id: str,
        *,
        model: dict[str, Any] | None = None,
        bill_no: str = "",
    ) -> ActionResult:
        """执行写动作（不自动重试，幂等由上层 WriteCoordinator 保证）。

        Args:
            action: 动作名（"save" / "submit" / "audit" / "unaudit" / "delete"）。
            form_id: 业务对象标识。
            model: save 必填，完整业务对象数据。
            bill_no: 非 save 必填，目标单据编号。
        """
        ...

    async def aclose(self) -> None:
        """释放底层资源（连接池等）。"""
        ...

    async def __aenter__(self) -> Self:
        """进入上下文（``async with`` 装配用）。"""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """退出上下文：等价于 ``aclose()``。"""
        ...
