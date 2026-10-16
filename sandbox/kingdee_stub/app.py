"""本地 K3Cloud 契约仿真沙箱（M1-T06a）。

定位：**契约测试替身**，不是「进程内 mock」。它遵守金蝶协议形状（URL 路径、
Cookie 会话、二维数组载荷、错误信封），因此能暴露 mock 暴露不了的一类缺陷：
URL 拼接错、Cookie 未携带、字段列序错位、错误信封解析错。真沙箱到位后，
只需把 ``KINGPILOT_KINGDEE_*`` 换 5 行即可切到真环境（协议形状不变）。

诚实局限（不粉饰）：字段全集、协议边角（TopRowCount 统计语义）、性能参数、
错误码具体取值均以官方文档与社区实测推断，未在真环境验证，已在 M1-T07 记录。

报文形状对齐（官方文档）：
- 登录请求 ``{"parameters": [账套ID, 用户名, 应用ID, 时间戳, 签名, lcId]}``
- 查询请求 ``{"data": {FormId, FieldKeys, FilterString, OrderString,
  TopRowCount, StartRow, Limit}}``（``FormId`` 在 ``data`` **里面**）
- 失败响应 HTTP **也是 200**，错误包在二维数组里（必须解析返回体判定）
"""

import hashlib
import hmac
import json
import operator
import re
import uuid
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kingdee.endpoints.sal_order import SAL_ORDER
from models.kingdee import JsonScalar
from sandbox.kingdee_stub.dataset import (
    ENTRY_FIELD_NAMES,
    ORDERS,
    SalesOrder,
    entry_value,
    has_entry_field,
    header_value,
)
from sandbox.kingdee_stub.filter_parser import FilterSyntaxError, parse_filter

# ── 仿真凭证（与真实密钥无关的占位值；.env 里切真环境时整块替换）──
SANDBOX_ACCT_ID = "sandbox_acct"
SANDBOX_USER = "demo"
SANDBOX_APP_ID = "sandbox_app"
SANDBOX_APP_SECRET = "sandbox_secret_not_a_real_credential"  # pragma: allowlist secret

SANDBOX_BASE_PATH = "/K3Cloud/Kingdee.BOS.WebApi.ServicesStub"
LOGIN_PATH = f"{SANDBOX_BASE_PATH}/AuthService.LoginBySign.common.kdsvc"
QUERY_PATH = f"{SANDBOX_BASE_PATH}/DynamicFormService.ExecuteBillQuery.common.kdsvc"

SESSION_COOKIE_NAME = "KDSVCSessionId"  # 金蝶会话 Cookie 名（官方实测）

_LOGIN_PARAM_COUNT = 6

# 错误码：INFERRED——官方未公开各错误码确切取值，此处取常见约定值，待真环境核对
_MSG_SESSION_LOST = 5
_MSG_FORM_NOT_FOUND = 4
_MSG_FIELD_NOT_FOUND = 9
_MSG_FILTER_SYNTAX = 10
_MSG_FILTER_FIELD = 11
_MSG_TOP_ROW_COUNT = 12
_MSG_ORDER_STRING = 13

app = FastAPI(title="KingPilot K3Cloud Contract Stub", docs_url=None, redoc_url=None)

# 已建立的会话（仿真端只判存在性，不设 TTL——会话过期场景由被测端 TTL 覆盖）
_sessions: set[str] = set()


def reset_sessions() -> None:
    """清空会话（测试 fixture 用；进程内可变状态需要显式复位）。"""
    _sessions.clear()


# ── 报文构造 ──


def _error_envelope(message: str, msg_code: int) -> list[Any]:
    """构造金蝶失败信封：``[[{"Result": {"ResponseStatus": {...}}}]]``（HTTP 200）。"""
    return [
        [
            {
                "Result": {
                    "ResponseStatus": {
                        "ErrorCode": msg_code,
                        "IsSuccess": False,
                        "Errors": [{"FieldName": "", "Message": message, "DIndex": 0}],
                        "SuccessEntitys": [],
                        "SuccessMessages": [],
                        "MsgCode": msg_code,
                    }
                }
            }
        ]
    ]


def _business_error(message: str, msg_code: int) -> JSONResponse:
    """业务错误统一以 HTTP 200 + 错误信封返回（金蝶真实行为，不能靠状态码判错）。"""
    return JSONResponse(content=_error_envelope(message, msg_code), status_code=200)


async def _read_json(request: Request) -> dict[str, Any] | None:
    """读取 JSON 请求体；非法 JSON 或非对象返回 ``None``。"""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# ── 字段取值与过滤求值 ──

_FIELD_TYPES: dict[str, str] = {field.name: field.data_type for field in SAL_ORDER.fields}
_HEADER_FIELD_NAMES: frozenset[str] = SAL_ORDER.selectable_fields - ENTRY_FIELD_NAMES


def _full_row(order: SalesOrder, entry_index: int | None) -> dict[str, JsonScalar]:
    """构造「全字段」行（含未在 FieldKeys 里的字段，供过滤/排序取值用）。

    Why 建全字段行而非只建请求列：真金蝶允许过滤字段不出现在 FieldKeys 里
    （如按 ``FCustomerID.FNumber`` 过滤却只显示 ``FBillNo``）。若只为请求列建行，
    这类过滤会静默失效——正是契约测试最该拦住的假绿。
    """
    row: dict[str, JsonScalar] = {name: header_value(order, name) for name in _HEADER_FIELD_NAMES}
    for name in ENTRY_FIELD_NAMES:
        row[name] = entry_value(order, entry_index, name) if entry_index is not None else None
    return row


def _build_rows(field_keys: list[str]) -> list[dict[str, JsonScalar]]:
    """按 FieldKeys 是否含明细字段决定行粒度：含则一单多行，否则一单一行。"""
    rows: list[dict[str, JsonScalar]] = []
    expand_entries = has_entry_field(field_keys)
    for order in ORDERS:
        if expand_entries:
            rows.extend(_full_row(order, index) for index in range(len(order.entries)))
        else:
            rows.append(_full_row(order, None))
    return rows


def _as_decimal(value: JsonScalar) -> Decimal | None:
    """把单元格值转为 Decimal（数值比较用，避免 float 精度误差）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _match_like(actual: str, pattern: str) -> bool:
    """SQL 通配匹配：``%`` 任意串、``_`` 单字符（其余字符按字面量）。"""
    regex = "".join(
        ".*" if char == "%" else "." if char == "_" else re.escape(char) for char in pattern
    )
    return re.fullmatch(regex, actual) is not None


# 比较符 → 运算函数（表驱动，避免 6 个 if 分支在数值/字符串两处重复）
_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq,
    "<>": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def _match(actual: JsonScalar, op: str, expected: str, data_type: str) -> bool:
    """单个条件求值；``actual`` 为 ``None`` 时实例值缺失，一律不匹配。"""
    if actual is None:
        return False
    if op == "LIKE":
        return _match_like(str(actual), expected)
    compare = _COMPARATORS[op]
    if data_type == "number":
        # 数值字段两侧都转 Decimal 再比（字符串直接比会得到 "9" > "10" 的错误结论）
        left, right = _as_decimal(actual), _as_decimal(expected)
        if left is None or right is None:
            return False
        return compare(left, right)
    return compare(str(actual), expected)


def _apply_filter(
    rows: list[dict[str, JsonScalar]], conditions: tuple[Any, ...]
) -> list[dict[str, JsonScalar]]:
    """逐条件过滤（AND 语义；条件间相互收敛）。"""
    for condition in conditions:
        data_type = _FIELD_TYPES.get(condition.field, "string")
        rows = [
            row
            for row in rows
            if _match(row.get(condition.field), condition.op, condition.value, data_type)
        ]
    return rows


# ── 排序 ──


def _parse_order_string(order_string: str) -> tuple[tuple[str, bool], ...]:
    """解析 ``OrderString``（如 ``"FBillNo"`` / ``"FDate DESC,FBillNo"``）。

    Raises:
        FilterSyntaxError: 排序字段含明细字段（一单多行无稳定语义）、字段不在白名单、
            或方向不是 ASC/DESC。
    """
    parsed: list[tuple[str, bool]] = []
    for raw in order_string.split(","):
        tokens = raw.strip().split()
        if not tokens:
            continue
        field = tokens[0]
        if field in ENTRY_FIELD_NAMES:
            raise FilterSyntaxError(f"仿真端不支持按明细字段排序: {field!r}")
        if field not in SAL_ORDER.selectable_fields:
            raise FilterSyntaxError(f"排序字段不在白名单: {field!r}")
        direction = tokens[1].upper() if len(tokens) > 1 else "ASC"
        if direction not in ("ASC", "DESC"):
            raise FilterSyntaxError(f"排序方向非法: {raw!r}")
        parsed.append((field, direction == "DESC"))
    return tuple(parsed)


def _sort_key(row: dict[str, JsonScalar], field: str) -> str | Decimal:
    """排序键：数值字段用 Decimal，其余用字符串（ISO 日期字符串序即时间序）。"""
    value = row.get(field)
    if value is None:
        return ""
    if _FIELD_TYPES.get(field) == "number":
        return _as_decimal(value) or Decimal(0)
    return str(value)


def _make_sort_key(field: str) -> Callable[[dict[str, JsonScalar]], str | Decimal]:
    """生成「按指定字段取排序键」的函数（供 ``list.sort(key=...)`` 用）。

    Why 不直接写 lambda：``lambda row, f=field: ...`` 带默认参数时 mypy 无法推断
    参数类型（strict 模式报 ``Cannot infer type of lambda``）；工厂函数可显式标注。
    """

    def key(row: dict[str, JsonScalar]) -> str | Decimal:
        return _sort_key(row, field)

    return key


def _apply_order(
    rows: list[dict[str, JsonScalar]], keys: tuple[tuple[str, bool], ...]
) -> list[dict[str, JsonScalar]]:
    """多键排序：按「逆序稳定排序」逐个应用，天然支持混合升降序。"""
    for field, descending in reversed(keys):
        rows.sort(key=_make_sort_key(field), reverse=descending)
    return rows


# ── 端点 ──


@app.post(LOGIN_PATH)
async def login(request: Request) -> JSONResponse:
    """LoginBySign：验签通过则下发 ``KDSVCSessionId`` 会话 Cookie。"""
    payload = await _read_json(request)
    params = payload.get("parameters") if payload else None
    if not isinstance(params, list) or len(params) != _LOGIN_PARAM_COUNT:
        return JSONResponse(
            content={"LoginResultType": -1, "Message": "登录参数格式错误"}, status_code=200
        )
    acct, user, app_id, timestamp, sign, _lc_id = params
    digest = hashlib.sha256(
        "".join(
            sorted(
                [
                    str(acct),
                    str(user),
                    str(app_id),
                    SANDBOX_APP_SECRET,
                    str(timestamp),
                ]
            )
        ).encode("utf-8")
    ).hexdigest()
    credentials_ok = (
        str(acct) == SANDBOX_ACCT_ID and str(user) == SANDBOX_USER and str(app_id) == SANDBOX_APP_ID
    )
    # 常量时间比对，避免签名校验引入时序侧信道（与实现层一致的谨慎）
    sign_ok = hmac.compare_digest(digest, str(sign))
    if not credentials_ok or not sign_ok:
        return JSONResponse(
            content={"LoginResultType": -1, "Message": "验签失败：账套/用户/应用ID或签名不正确"},
            status_code=200,
        )
    token = uuid.uuid4().hex
    _sessions.add(token)
    response = JSONResponse(content={"LoginResultType": 1, "Context": {}}, status_code=200)
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True)
    return response


def _validate_query(data: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """校验查询请求；返回规范化后的参数 dict，或直接可返回的错误响应。"""
    form_id = data.get("FormId")
    if form_id != SAL_ORDER.form_id:
        return _business_error(f"业务对象标识为 {form_id} 的单据不存在", _MSG_FORM_NOT_FOUND)

    raw_keys = data.get("FieldKeys")
    if not isinstance(raw_keys, str) or not raw_keys.strip():
        return _business_error("FieldKeys 不能为空", _MSG_FIELD_NOT_FOUND)
    field_keys = [key.strip() for key in raw_keys.split(",")]
    unknown = [key for key in field_keys if key not in SAL_ORDER.selectable_fields]
    if unknown:
        return _business_error(f"元数据中标识为{unknown[0]}的字段不存在", _MSG_FIELD_NOT_FOUND)

    filter_string = data.get("FilterString") or ""
    if not isinstance(filter_string, str):
        return _business_error("FilterString 类型非法", _MSG_FILTER_SYNTAX)
    try:
        conditions = parse_filter(filter_string)
    except FilterSyntaxError as exc:
        return _business_error(str(exc), _MSG_FILTER_SYNTAX)
    for condition in conditions:
        if condition.field not in SAL_ORDER.filterable_fields:
            return _business_error(f"字段 {condition.field} 不可作为过滤条件", _MSG_FILTER_FIELD)

    if data.get("TopRowCount", 0) != 0:
        # TopRowCount 是「只取总行数」的统计语义，仿真端未实现——fail-closed 明确报错，
        # 不返回看似正常的空结果（否则被测代码会误以为统计功能可用）
        return _business_error("仿真端未实现 TopRowCount 统计语义", _MSG_TOP_ROW_COUNT)

    order_string = data.get("OrderString") or ""
    try:
        order_keys = _parse_order_string(order_string) if order_string else ()
    except FilterSyntaxError as exc:
        return _business_error(str(exc), _MSG_ORDER_STRING)

    start_row = data.get("StartRow", 0)
    limit = data.get("Limit", 100)
    if not isinstance(start_row, int) or start_row < 0 or not isinstance(limit, int) or limit < 0:
        return _business_error("StartRow / Limit 必须为非负整数", _MSG_FIELD_NOT_FOUND)

    return {
        "field_keys": field_keys,
        "conditions": conditions,
        "order_keys": order_keys,
        "start_row": start_row,
        "limit": limit,
    }


@app.post(QUERY_PATH)
async def execute_bill_query(request: Request) -> JSONResponse:
    """ExecuteBillQuery：校验会话与字段白名单后返回二维数组（列序严格 = FieldKeys 序）。"""
    if request.cookies.get(SESSION_COOKIE_NAME) not in _sessions:
        return _business_error("会话信息丢失，请重新登录", _MSG_SESSION_LOST)

    payload = await _read_json(request)
    data = payload.get("data") if payload else None
    if not isinstance(data, dict):
        return _business_error("请求体缺少 data 参数", _MSG_FORM_NOT_FOUND)

    validated = _validate_query(data)
    if isinstance(validated, JSONResponse):
        return validated

    rows = _build_rows(validated["field_keys"])
    rows = _apply_filter(rows, validated["conditions"])
    rows = _apply_order(rows, validated["order_keys"])
    start, limit = validated["start_row"], validated["limit"]
    rows = rows[start:] if limit == 0 else rows[start : start + limit]

    field_keys: list[str] = validated["field_keys"]
    projected = [[row.get(key) for key in field_keys] for row in rows]
    return JSONResponse(content=projected, status_code=200)
