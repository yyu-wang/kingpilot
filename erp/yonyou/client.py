"""用友客户端骨架：满足 ``ErpClient`` 协议，但不连接真实环境。

Why 骨架也实现全部协议方法（而不是留空类）：
``isinstance(client, ErpClient)`` 依赖方法存在性——骨架能通过协议检查，
才证明「新增厂商 = 实现协议」这条路成立；留空类会让演示变成口头承诺。

Why 未接入时抛异常而不是返回空结果：
空结果会被上层当成「查询成功但没有数据」，用户看到的是「没查到」而不是
「该厂商未接入」；配置类问题必须尽早、明确地失败（与项目 fail-closed 一致）。
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from erp.types import BillQuerySpec
from infrastructure.exceptions import KingPilotError
from models.kingdee import ActionResult, BillQueryResult

if TYPE_CHECKING:
    from config.settings import Settings
    from infrastructure.secrets import SecretProvider

__all__ = ["YonyouClient", "YonyouNotConfiguredError"]


class YonyouNotConfiguredError(KingPilotError):
    """用友骨架尚未接入真实环境（配置/实现缺失，非运行时业务错误）。

    Why 放厂商包而非 ``infrastructure/exceptions.py``：
    厂商专属异常随厂商包走，新增厂商时不必改动核心异常文件；仍继承
    ``KingPilotError``，全局异常处理与日志分层不变。
    """


class YonyouClient:
    """用友 U8/U9 适配器骨架（``ErpClient`` 实现）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        secrets: SecretProvider | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """骨架未持有连接池，无需释放（保留方法以满足协议）。"""

    async def query(self, spec: BillQuerySpec) -> BillQueryResult:
        """未接入：明确失败，不返回空结果。"""
        raise YonyouNotConfiguredError(
            f"用友适配器骨架未接入真实环境（form_id={spec.form_id}）",
            context={"vendor": "yonyou", "form_id": spec.form_id, "operation": "query"},
        )

    async def invoke_action(
        self,
        action: str,
        form_id: str,
        *,
        model: dict[str, Any] | None = None,
        bill_no: str = "",
    ) -> ActionResult:
        """未接入：明确失败，不返回伪成功。"""
        raise YonyouNotConfiguredError(
            f"用友适配器骨架未接入真实环境（{form_id}.{action}）",
            context={
                "vendor": "yonyou",
                "form_id": form_id,
                "action": action,
                "operation": "invoke_action",
            },
        )
