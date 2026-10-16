"""M1-T04：KingdeeClient 单测（respx / MockTransport，零网络）。

覆盖任务卡验收点：200 / 4xx / 5xx / 超时 / 限流退避，
外加三层失败处理的边界（会话失效重登一次、信号量退避期释放、并发峰值）。
"""

import asyncio
from typing import Any

import httpx
import pytest
import respx

from config.settings import Settings
from infrastructure.exceptions import KingdeeAPIError, KingdeeAuthError, KingdeeRateLimitError
from kingdee.client import KingdeeClient
from models.kingdee import BillQueryParams

_SERVER = "https://test.ik3cloud.com"
_LOGIN_URL = (
    f"{_SERVER}/K3Cloud/Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginBySign.common.kdsvc"
)
_QUERY_URL = (
    f"{_SERVER}/K3Cloud/Kingdee.BOS.WebApi.ServicesStub"
    f".DynamicFormService.ExecuteBillQuery.common.kdsvc"
)
_ACCT_ID = "66583a5b4c621c"  # pragma: allowlist secret（官方文档示例账套ID，非真凭证）
_SECRET = "super_secret_value_12345"  # pragma: allowlist secret（测试假值，非真凭证）
_LOGIN_OK = {"LoginResultType": 1, "Context": {}}
_SUCCESS_ROWS = [["26091", "2026-09-10T00:00:00", "测试客户001", "C"]]
_ERROR_RESPONSE = [
    [
        {
            "Result": {
                "ResponseStatus": {
                    "ErrorCode": 9,
                    "IsSuccess": False,
                    "Errors": [
                        {"FieldName": "", "Message": "元数据中标识为FNotAField的字段不存在"}
                    ],
                    "MsgCode": 9,
                }
            }
        }
    ]
]


async def _no_sleep(_: float) -> None:
    """零等待休眠（测试用，避免退避拖慢用例）。"""


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "kingdee_server_url": _SERVER,
        "kingdee_acct_id": _ACCT_ID,
        "kingdee_user": "demo",
        "kingdee_app_id": "test_app_id",
        "kingdee_app_secret": _SECRET,
        "kingdee_max_retries": 2,
    }
    base.update(overrides)
    return Settings(**base)


def _client(**overrides: Any) -> KingdeeClient:
    """确定性客户端：抖动取半、休眠零等待。"""
    return KingdeeClient(
        _settings(**overrides),
        jitter=lambda delay: delay / 2,
        sleep=_no_sleep,
    )


def _params() -> BillQueryParams:
    return BillQueryParams(
        form_id="SAL_Order", field_keys="FBillNo,FDate,FCustomerID.FName,FDocumentStatus"
    )


# ── 成功路径 ──


@respx.mock
async def test_query_success_returns_typed_result() -> None:
    """成功返回类型化结果（规范 2：不返回裸二维数组）。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    respx.post(_QUERY_URL).mock(return_value=httpx.Response(200, json=_SUCCESS_ROWS))
    async with _client() as client:
        result = await client.execute_bill_query(_params())
    assert result.form_id == "SAL_Order"
    assert result.field_keys == ["FBillNo", "FDate", "FCustomerID.FName", "FDocumentStatus"]
    assert result.rows[0]["FCustomerID.FName"] == "测试客户001"
    assert result.row_count == 1


@respx.mock
async def test_query_body_wrapped_in_data_envelope() -> None:
    """信封锚定：查询参数必须包在 data 内层（FormId 在 data 里面）。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(return_value=httpx.Response(200, json=_SUCCESS_ROWS))
    async with _client() as client:
        await client.execute_bill_query(_params())
    body = route.calls[0].request.read().decode()
    assert '"data"' in body and '"FormId":"SAL_Order"' in body
    assert body.index('"data"') < body.index('"FormId"')


@respx.mock
async def test_login_happens_before_query_and_is_cached() -> None:
    """调用前自动建会话；会话有效期内多次查询不重复登录。"""
    login = respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    respx.post(_QUERY_URL).mock(return_value=httpx.Response(200, json=_SUCCESS_ROWS))
    async with _client() as client:
        await client.execute_bill_query(_params())
        await client.execute_bill_query(_params())
    assert login.call_count == 1


# ── 瞬时故障：退避重试 ──


@respx.mock
async def test_http_500_then_success_retries() -> None:
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=_SUCCESS_ROWS)]
    )
    async with _client() as client:
        result = await client.execute_bill_query(_params())
    assert route.call_count == 2
    assert result.row_count == 1


@respx.mock
async def test_http_500_exhausted_raises_api_error() -> None:
    """5xx 用尽重试 → KingdeeAPIError，请求次数 = 1 + max_retries。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(return_value=httpx.Response(500))
    async with _client() as client:
        with pytest.raises(KingdeeAPIError, match="HTTP 500"):
            await client.execute_bill_query(_params())
    assert route.call_count == 3


@respx.mock
async def test_timeout_retries_then_success() -> None:
    """超时属瞬时故障，允许重试（查询是幂等操作）。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(
        side_effect=[httpx.ConnectTimeout("boom"), httpx.Response(200, json=_SUCCESS_ROWS)]
    )
    async with _client() as client:
        result = await client.execute_bill_query(_params())
    assert route.call_count == 2
    assert result.row_count == 1


@respx.mock
async def test_connect_error_exhausted_raises_api_error_without_leaking_secret() -> None:
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    respx.post(_QUERY_URL).mock(side_effect=httpx.ConnectError("down"))
    async with _client() as client:
        with pytest.raises(KingdeeAPIError) as excinfo:
            await client.execute_bill_query(_params())
    assert _SECRET not in str(excinfo.value)


# ── 限流 ──


@respx.mock
async def test_rate_limit_then_success_retries() -> None:
    """429 是唯一具备瞬时语义的 4xx，允许退避重试。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=_SUCCESS_ROWS)]
    )
    async with _client() as client:
        result = await client.execute_bill_query(_params())
    assert route.call_count == 2
    assert result.row_count == 1


@respx.mock
async def test_rate_limit_exhausted_raises_rate_limit_error() -> None:
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    respx.post(_QUERY_URL).mock(return_value=httpx.Response(429))
    async with _client() as client:
        with pytest.raises(KingdeeRateLimitError):
            await client.execute_bill_query(_params())


@respx.mock
async def test_retry_after_header_is_respected() -> None:
    """429 带 Retry-After 时优先遵从其秒数（此处抖动取原值以断言精确延时）。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    respx.post(_QUERY_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json=_SUCCESS_ROWS),
        ]
    )
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    client = KingdeeClient(_settings(), jitter=lambda delay: delay, sleep=record)
    async with client:
        await client.execute_bill_query(_params())
    assert delays == [3.0]


# ── 不重试：确定性失败 ──


@respx.mock
async def test_client_error_400_is_not_retried() -> None:
    """4xx（非 429）重试无意义，必须立即失败（早暴露、不浪费配额）。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(return_value=httpx.Response(400))
    async with _client() as client:
        with pytest.raises(KingdeeAPIError, match="HTTP 400"):
            await client.execute_bill_query(_params())
    assert route.call_count == 1


@respx.mock
async def test_business_error_carries_official_message() -> None:
    """HTTP 200 但 IsSuccess=false → KingdeeAPIError，携带 MsgCode 与官方原文。"""
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(return_value=httpx.Response(200, json=_ERROR_RESPONSE))
    async with _client() as client:
        with pytest.raises(KingdeeAPIError) as excinfo:
            await client.execute_bill_query(_params())
    message = str(excinfo.value)
    assert "MsgCode=9" in message and "FNotAField" in message
    assert _SECRET not in message
    assert route.call_count == 1  # 业务错误不重试


# ── 会话失效：重登一次后重放（不消耗重试预算）──


def _session_lost_response() -> httpx.Response:
    return httpx.Response(
        200,
        json=[
            [
                {
                    "Result": {
                        "ResponseStatus": {
                            "IsSuccess": False,
                            "Errors": [{"FieldName": "", "Message": "会话信息丢失，请重新登录"}],
                            "MsgCode": 5,
                        }
                    }
                }
            ]
        ],
    )


@respx.mock
async def test_session_invalid_relogin_once_and_replay() -> None:
    login = respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(
        side_effect=[_session_lost_response(), httpx.Response(200, json=_SUCCESS_ROWS)]
    )
    async with _client() as client:
        result = await client.execute_bill_query(_params())
    assert result.row_count == 1
    assert route.call_count == 2  # 原请求 + 重登后重放
    assert login.call_count == 2  # 首次登录 + 重登


@respx.mock
async def test_session_invalid_twice_raises_auth_error_no_loop() -> None:
    """重登后仍失效 → 直接抛错，绝不无限重登（防死循环）。"""
    login = respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(return_value=_session_lost_response())
    async with _client() as client:
        with pytest.raises(KingdeeAuthError, match="重登后仍失效"):
            await client.execute_bill_query(_params())
    assert route.call_count == 2
    assert login.call_count == 2


@respx.mock
async def test_http_401_triggers_relogin_and_replay() -> None:
    """HTTP 401 也识别为会话失效（真环境若用 401 表达时本条兜底）。"""
    login = respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    route = respx.post(_QUERY_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=_SUCCESS_ROWS)]
    )
    async with _client() as client:
        result = await client.execute_bill_query(_params())
    assert result.row_count == 1
    assert route.call_count == 2 and login.call_count == 2


# ── 并发与信号量 ──


@respx.mock
async def test_semaphore_released_during_backoff() -> None:
    """退避等待期间信号量必须已归还（max_concurrency=1 时最易暴露死锁式设计）。

    观测点在注入的 ``sleep`` 回调：能走到 sleep，说明 ``_post_once`` 的
    ``async with semaphore`` 已退出——即「退避时不再占着车道」。
    """
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(200, json=_LOGIN_OK))
    respx.post(_QUERY_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=_SUCCESS_ROWS)]
    )
    observed: list[int] = []
    holder: dict[str, KingdeeClient] = {}

    async def record(_: float) -> None:
        observed.append(holder["client"]._semaphore._value)

    client = KingdeeClient(
        _settings(kingdee_max_concurrency=1, kingdee_max_retries=1),
        jitter=lambda delay: delay / 2,
        sleep=record,
    )
    holder["client"] = client
    async with client:
        result = await client.execute_bill_query(_params())
    assert observed == [1]  # 退避时信号量已释放
    assert result.row_count == 1


async def test_concurrency_peak_respects_limit() -> None:
    """并发峰值不超过 max_concurrency（MockTransport 制造真实重叠）。"""
    state = {"current": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "AuthService" in request.url.path:
            return httpx.Response(200, json=_LOGIN_OK)
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1
        return httpx.Response(200, json=_SUCCESS_ROWS)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KingdeeClient(_settings(kingdee_max_concurrency=2), http=http, sleep=_no_sleep)
    try:
        results = await asyncio.gather(*[client.execute_bill_query(_params()) for _ in range(6)])
    finally:
        await http.aclose()
    assert len(results) == 6
    assert state["peak"] <= 2
