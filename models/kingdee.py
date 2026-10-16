"""金蝶 K3Cloud WebAPI 协议模型（M1-T02）。

对齐官方文档与实测报文（vip.kingdee.com《WebApi查询接口使用说明》
及 ExecuteBillQuery 实测文章，2026-09）：

- 请求：``FormId`` 在 ``data`` **里面**；分页用 ``StartRow``（行偏移，从 0 起）
  + ``Limit``（``0`` = 不限）；排序用 ``OrderString``。
  注意：方案 §3.1 写的 ``PageSize/PageIndex/SortString`` 与真协议不符，
  本文件按真协议命名（决策 D1，见 M1-T02 设计）。
- 成功响应：二维数组，**无字段名**，列顺序 = ``FieldKeys`` 顺序，取不到值为 ``null``。
- 失败响应：**HTTP 状态码也是 200**，错误对象包在二维数组里
  ``[[{"Result": {"ResponseStatus": {...}}}]]``——必须解析返回体判 ``IsSuccess``。

职责边界（防腐层原则）：本层只保真搬运协议形状，不做语义解释——
数量精度（金蝶返回 10 位小数字符串）转换归业务层；异常归一化为
``KingdeeAPIError`` 归 T04 client（§3.4：重试与归一化只在 client 一处）。
"""

from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from models.base import KingPilotBaseModel

# JSON 标量（金蝶查询行单元格的全部可能类型；嵌套对象只会出现在 View 接口，不在本层）
type JsonScalar = str | int | float | bool | None


class DocumentStatus(StrEnum):
    """``FDocumentStatus`` 值域（官方文档明确枚举）。

    对接时一般只取 ``AUDITED``（已审核）的单据。
    """

    DRAFT = "Z"  # 暂存
    CREATED = "A"  # 创建
    AUDITING = "B"  # 审核中
    AUDITED = "C"  # 已审核
    RE_AUDIT = "D"  # 重新审核


class BillQueryParams(KingPilotBaseModel):
    """``ExecuteBillQuery`` 的 ``data`` 参数（与金蝶报文 1:1）。

    Python 侧用 snake_case，序列化时 ``model_dump(by_alias=True)`` 输出金蝶键名；
    构造时两种键名均可（``populate_by_name=True``）。

    Why 不校验 FilterString 安全性：那是 M4 Guard 2（AST 白名单）的职责，
    本层只保证形状合法，职责分离避免双重防线互相稀释。
    """

    model_config = ConfigDict(populate_by_name=True)

    form_id: str = Field(alias="FormId", min_length=1)  # 业务对象标识，如 "SAL_Order"
    field_keys: str = Field(
        alias="FieldKeys", min_length=1
    )  # 逗号分隔；关联字段用点号 FCustomerID.FName
    filter_string: str = Field(default="", alias="FilterString")  # 类 SQL，须先过 Guard 2
    order_string: str = Field(default="", alias="OrderString")  # 如 "FDate DESC"
    top_row_count: int = Field(default=0, alias="TopRowCount", ge=0)  # 预留（总行数统计）
    start_row: int = Field(default=0, alias="StartRow", ge=0)  # 行偏移，从 0 开始
    limit: int = Field(default=100, alias="Limit", ge=0)  # 最多返回行数；0 = 不限（金蝶语义）


class LoginResult(KingPilotBaseModel):
    """``LoginBySign`` 响应（官方文档：成功 ``LoginResultType == 1``）。

    会话本体是响应 Set-Cookie 写入 cookie jar 的 ``KDSVCSessionId``（Cookie 会话，
    非 Bearer Token），本模型只承载判定字段；``context`` 原样保留备查。
    """

    model_config = ConfigDict(populate_by_name=True)

    login_result_type: int = Field(alias="LoginResultType")
    message: str | None = Field(default=None, alias="Message")
    message_code: str | None = Field(default=None, alias="MessageCode")
    context: dict[str, Any] | None = Field(default=None, alias="Context")

    @property
    def is_success(self) -> bool:
        """官方成功判定：LoginResultType == 1。"""
        return self.login_result_type == 1


class ErrorItem(KingPilotBaseModel):
    """单条错误明细。"""

    model_config = ConfigDict(populate_by_name=True)

    field_name: str = Field(default="", alias="FieldName")
    message: str = Field(default="", alias="Message")
    d_index: int = Field(default=0, alias="DIndex")


class SuccessEntity(KingPilotBaseModel):
    """单条成功实体回执（写操作返回；查询失败报文里通常为空数组）。"""

    model_config = ConfigDict(populate_by_name=True)

    id: int | str = Field(default=0, alias="Id")
    number: str = Field(default="", alias="Number")
    d_index: int = Field(default=0, alias="DIndex")


class ResponseStatus(KingPilotBaseModel):
    """``ResponseStatus`` 业务状态块（金蝶所有接口通用）。

    注意：金蝶官方拼写就是 ``SuccessEntitys``（应为 Entities），
    alias 必须保留官方拼写否则反序列化失败——这是对接方无权修正的错误。
    """

    model_config = ConfigDict(populate_by_name=True)

    error_code: int = Field(default=0, alias="ErrorCode")
    is_success: bool = Field(alias="IsSuccess")
    errors: list[ErrorItem] = Field(default_factory=list, alias="Errors")
    success_entitys: list[SuccessEntity] = Field(default_factory=list, alias="SuccessEntitys")
    success_messages: list[ErrorItem] = Field(default_factory=list, alias="SuccessMessages")
    msg_code: int = Field(default=0, alias="MsgCode")

    @property
    def error_message(self) -> str:
        """拼接全部错误消息（官方判错函数同款语义）。"""
        return "；".join(e.message for e in self.errors if e.message)


class ResultEnvelope(KingPilotBaseModel):
    """``{"ResponseStatus": ...}`` 内层。"""

    model_config = ConfigDict(populate_by_name=True)

    response_status: ResponseStatus = Field(alias="ResponseStatus")


class ErrorEnvelope(KingPilotBaseModel):
    """``{"Result": {"ResponseStatus": ...}}`` 错误信封。"""

    model_config = ConfigDict(populate_by_name=True)

    result: ResultEnvelope = Field(alias="Result")


def extract_response_status(raw: object) -> ResponseStatus | None:
    """从 ExecuteBillQuery 原始返回中检测业务错误；无错误返回 ``None``。

    Why 显式判别而非 Pydantic Union：成功与失败**同为二维数组**，仅首元素
    类型不同（标量 vs 错误对象），Union 逐个试配会产生误导性报错；
    显式形状检测意图清晰、fail-closed。
    正常行首元素只会是标量/None（对象只会出现在 View 接口），不会误判。

    Args:
        raw: HTTP 响应 JSON 反序列化后的对象。

    Returns:
        检测到 ``IsSuccess=false`` 时返回 ``ResponseStatus``，否则 ``None``。
    """
    if (
        isinstance(raw, list)
        and raw
        and isinstance(raw[0], list)
        and raw[0]
        and isinstance(raw[0][0], dict)
    ):
        envelope = ErrorEnvelope.model_validate(raw[0][0])
        status = envelope.result.response_status
        if not status.is_success:
            return status
    return None


def rows_to_dicts(field_keys: str, rows: list[list[JsonScalar]]) -> list[dict[str, JsonScalar]]:
    """二维数组 → 对象数组，键 = ``FieldKeys`` 列名（官方推荐转换法）。

    Why 必须转换：按位置取值（``row[2]``）在 ``FieldKeys`` 调整后静默错位，
    是金蝶对接最高频的自伤 bug；转成 dict 后按键取值，列序变化即时报错。

    Args:
        field_keys: 请求时的 ``FieldKeys`` 原串（逗号分隔）。
        rows: 成功响应的二维数组。

    Returns:
        键为列名（关联字段保留原样如 ``FCustomerID.FName``）、值为单元格原值
        （``None`` 保留；数量 10 位小数字符串原样不转 float，精度处理归业务层）。

    Raises:
        ValueError: FieldKeys 含空列名，或行列数与列数不符（协议被破坏，fail-closed）。
    """
    keys = [k.strip() for k in field_keys.split(",")]
    if any(not k for k in keys):
        raise ValueError(f"FieldKeys 含空列名: {field_keys!r}")
    result: list[dict[str, JsonScalar]] = []
    for i, row in enumerate(rows):
        if len(row) != len(keys):
            raise ValueError(f"第 {i} 行列数({len(row)})与 FieldKeys 列数({len(keys)})不符")
        result.append(dict(zip(keys, row, strict=True)))
    return result
