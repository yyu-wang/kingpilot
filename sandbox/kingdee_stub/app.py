"""本地 K3Cloud 契约仿真沙箱（M1-T06a，M2-T04 拆分查询引擎）。

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

查询语义（白名单/过滤/排序/行构造）见 ``query_engine.py``，本模块只管 HTTP。
"""

import hashlib
import hmac
import json
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sandbox.kingdee_stub.filter_parser import FilterSyntaxError, parse_filter
from sandbox.kingdee_stub.query_engine import (
    FORMS,
    FormModel,
    apply_filter,
    apply_order,
    parse_order_string,
)

# ── 仿真凭证（与真实密钥无关的占位值；.env 里切真环境时整块替换）──
SANDBOX_ACCT_ID = "sandbox_acct"
SANDBOX_USER = "demo"
SANDBOX_APP_ID = "sandbox_app"
SANDBOX_APP_SECRET = "sandbox_secret_not_a_real_credential"  # pragma: allowlist secret

# 路径拼接必须是**点号**：金蝶 URL 的服务全名是 Kingdee.BOS.WebApi.ServicesStub.AuthService…
# （T06b 真 HTTP 验收抓出过此错：斜杠拼接在 T06a 单测里全绿——测试与实现共用同一
# 常量属同义反复，只有 KingdeeClient 按真协议发请求才能暴露。教训见 M1-T07 小结）
SANDBOX_BASE_PATH = "/K3Cloud/Kingdee.BOS.WebApi.ServicesStub"
LOGIN_PATH = f"{SANDBOX_BASE_PATH}.AuthService.LoginBySign.common.kdsvc"
QUERY_PATH = f"{SANDBOX_BASE_PATH}.DynamicFormService.ExecuteBillQuery.common.kdsvc"

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
    """校验查询请求；返回规范化后的参数 dict（含命中 form 模型），或错误响应。"""
    form_id = data.get("FormId")
    form = FORMS.get(str(form_id))
    if form is None:
        return _business_error(f"业务对象标识为 {form_id} 的单据不存在", _MSG_FORM_NOT_FOUND)

    raw_keys = data.get("FieldKeys")
    if not isinstance(raw_keys, str) or not raw_keys.strip():
        return _business_error("FieldKeys 不能为空", _MSG_FIELD_NOT_FOUND)
    field_keys = [key.strip() for key in raw_keys.split(",")]
    unknown = [key for key in field_keys if key not in form.descriptor.selectable_fields]
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
        if condition.field not in form.descriptor.filterable_fields:
            return _business_error(f"字段 {condition.field} 不可作为过滤条件", _MSG_FILTER_FIELD)

    if data.get("TopRowCount", 0) != 0:
        # TopRowCount 是「只取总行数」的统计语义，仿真端未实现——fail-closed 明确报错，
        # 不返回看似正常的空结果（否则被测代码会误以为统计功能可用）
        return _business_error("仿真端未实现 TopRowCount 统计语义", _MSG_TOP_ROW_COUNT)

    order_string = data.get("OrderString") or ""
    try:
        order_keys = (
            parse_order_string(order_string, form.descriptor.selectable_fields, form.non_sortable)
            if order_string
            else ()
        )
    except FilterSyntaxError as exc:
        return _business_error(str(exc), _MSG_ORDER_STRING)

    start_row = data.get("StartRow", 0)
    limit = data.get("Limit", 100)
    if not isinstance(start_row, int) or start_row < 0 or not isinstance(limit, int) or limit < 0:
        return _business_error("StartRow / Limit 必须为非负整数", _MSG_FIELD_NOT_FOUND)

    return {
        "form": form,
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

    form: FormModel = validated["form"]
    rows = form.build_rows(validated["field_keys"])
    rows = apply_filter(rows, validated["conditions"], form.field_types)
    rows = apply_order(rows, validated["order_keys"], form.field_types)
    start, limit = validated["start_row"], validated["limit"]
    rows = rows[start:] if limit == 0 else rows[start : start + limit]

    field_keys: list[str] = validated["field_keys"]
    projected = [[row.get(key) for key in field_keys] for row in rows]
    return JSONResponse(content=projected, status_code=200)
