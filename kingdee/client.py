"""金蝶异步客户端（M1-T04）。

组装 T02（协议模型）与 T03（签名登录），对外只暴露类型化方法；
业务代码禁止直接出现 httpx 调用（M1 模块规范 1）。

三层失败处理（互不混淆，本项目核心设计之一）：
1. **瞬时故障**（超时/连接错误/5xx/429）→ 退避重试，消耗重试预算；
   429 是唯一具备瞬时语义的 4xx，用尽后抛 ``KingdeeRateLimitError``。
2. **会话失效**（HTTP 401 或错误原文含会话关键词）→ ``auth.invalidate()``
   重登后重放**一次**，**不消耗**重试预算；二次失效直接抛错，防死循环。
3. **业务错误 / 其它 4xx** → 立即抛 ``KingdeeAPIError``，不重试
   （重试只需重复失败，且会拖长失败暴露时间）。

Why 分三层：统一 ``for i in range(3)`` 会把「字段名写错」这类确定性失败
重试三次——延迟失败、污染日志、浪费配额。重试的前提是失败可自愈。
"""

import asyncio
import random
import time
from collections.abc import Callable, Coroutine
from enum import StrEnum
from types import TracebackType
from typing import Any, Self

import httpx

from config.settings import Settings
from infrastructure.exceptions import KingdeeAPIError, KingdeeAuthError, KingdeeRateLimitError
from infrastructure.secrets import SecretProvider
from kingdee.auth import KingdeeAuth
from models.kingdee import BillQueryParams, BillQueryResult, extract_response_status, rows_to_dicts

# 协议与策略常量（规范：禁止魔法数字/字符串）
_QUERY_PATH = (
    "/K3Cloud/Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.ExecuteBillQuery.common.kdsvc"
)
_BASE_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 8.0
_RATE_LIMIT_STATUS = 429
_SESSION_INVALID_STATUS = 401
# INFERRED: 会话失效信号取自社区实测报错原文（“会话信息丢失，请重新登录”），
# 真环境联调时核对；若真金蝶改用 HTTP 401 表达，本元组退化为兜底。
_SESSION_LOSS_KEYWORDS = ("会话", "重新登录", "SessionId")


class _Verdict(StrEnum):
    """单次响应的分类结果（决定走重试/重登/直接抛错）。"""

    SUCCESS = "success"
    RETRYABLE = "retryable"
    SESSION_INVALID = "session_invalid"
    BUSINESS_ERROR = "business_error"
    HTTP_ERROR = "http_error"


class KingdeeClient:
    """金蝶云星空异步客户端。

    自建并持有 ``httpx.AsyncClient``（配置显式超时），构造后交给 ``KingdeeAuth``
    共享同一 cookie jar 与连接池——避免 auth/client 互相依赖。

    Args:
        settings: 全局配置（超时/并发/重试参数与凭证来源）。
        http: 可选注入的 httpx 客户端（测试或复用连接池用）；不传则自建并负责关闭。
        clock: 时间源（测试用假钟）。
        jitter: 退避抖动函数，默认满抖动 ``random.uniform(0, d)``；测试注入
            ``lambda d: d / 2`` 以获得确定性断言（零 flaky）。
        sleep: 休眠函数，默认 ``asyncio.sleep``（测试零等待）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[float], float] | None = None,
        sleep: Callable[[float], Coroutine[Any, Any, None]] | None = None,
        secrets: SecretProvider | None = None,
    ) -> None:
        self._settings = settings
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=settings.kingdee_timeout_seconds)
        self._auth = KingdeeAuth(settings, self._http, clock=clock, secrets=secrets)
        self._semaphore = asyncio.Semaphore(settings.kingdee_max_concurrency)
        self._jitter = jitter or (lambda delay: random.uniform(0, delay))
        self._sleep = sleep or asyncio.sleep

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
        """关闭自建的连接池（注入的客户端由调用方负责）。"""
        if self._owns_http:
            await self._http.aclose()

    # ── 公开 API ──

    async def execute_bill_query(self, params: BillQueryParams) -> BillQueryResult:
        """执行单据查询（``ExecuteBillQuery``，幂等，允许自动重试）。

        Args:
            params: 查询参数。``filter_string`` 必须先通过 Guard 2（M4）校验；
                ``limit`` 硬上限 10000 由 Guard 4（M4）负责，本层不越权拦截。

        Returns:
            类型化结果（含按键转换后的行数据），不返回裸二维数组。

        Raises:
            KingdeeAuthError: 凭证缺失、登录被拒、或重登后会话仍失效。
            KingdeeRateLimitError: 限流且退避至重试上限。
            KingdeeAPIError: 传输失败、非可重试 HTTP 错误、或业务返回 IsSuccess=false。
        """
        url = self._settings.kingdee_server_url.rstrip("/") + _QUERY_PATH
        # 信封：金蝶要求查询参数包在 data 内层（BillQueryParams 即该内层对象）
        payload = {"data": params.model_dump(by_alias=True)}
        context: dict[str, Any] = {"operation": "ExecuteBillQuery", "form_id": params.form_id}

        await self._auth.ensure_session()
        relogged = False  # 会话失效只重放一次：防「重登→再失效」死循环
        attempt = 0
        while True:
            try:
                response = await self._post_once(url, payload)
            except httpx.HTTPError as exc:
                if attempt >= self._settings.kingdee_max_retries:
                    raise KingdeeAPIError(
                        f"金蝶请求失败: {type(exc).__name__}", context=context
                    ) from exc
                await self._backoff(attempt)
                attempt += 1
                continue

            verdict = self._classify(response)

            if verdict is _Verdict.SUCCESS:
                return self._to_result(params, response.json())

            if verdict is _Verdict.SESSION_INVALID:
                if relogged:
                    raise KingdeeAuthError("金蝶会话重登后仍失效，停止重试", context=context)
                relogged = True
                self._auth.invalidate()
                await self._auth.ensure_session()
                continue  # 不消耗重试预算

            if verdict is _Verdict.BUSINESS_ERROR:
                status = extract_response_status(response.json())
                raise KingdeeAPIError(
                    f"金蝶业务错误 MsgCode={getattr(status, 'msg_code', '')}: "
                    f"{getattr(status, 'error_message', '')}",
                    context=context,
                )

            if verdict is _Verdict.HTTP_ERROR:
                # 非可重试的 HTTP 失败（如 400/403）：响应体非业务 JSON，不解析
                raise KingdeeAPIError(f"金蝶返回 HTTP {response.status_code}", context=context)

            # RETRYABLE：429 可退避重试，其它 4xx/5xx 走瞬时层
            if attempt >= self._settings.kingdee_max_retries:
                if response.status_code == _RATE_LIMIT_STATUS:
                    raise KingdeeRateLimitError(
                        f"金蝶限流且重试{self._settings.kingdee_max_retries}次未恢复",
                        context=context,
                    )
                raise KingdeeAPIError(f"金蝶返回 HTTP {response.status_code}", context=context)
            await self._backoff(attempt, response=response)
            attempt += 1

    # ── 内部实现 ──

    async def _post_once(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        """单次 HTTP 尝试。

        信号量只在此处持有——退避等待期间必须释放，
        否则一个失败的请求会占住车道、拖死整个并发池。
        """
        async with self._semaphore:
            return await self._http.post(url, json=payload)

    def _classify(self, response: httpx.Response) -> _Verdict:
        """把单次响应归入五类之一（分类集中在此，便于审阅与扩展）。"""
        if response.status_code == _SESSION_INVALID_STATUS:
            return _Verdict.SESSION_INVALID
        if response.status_code >= 400:
            # 401 会话失效；429/5xx 瞬时故障可重试；其余 4xx 是确定性失败
            if response.status_code == _RATE_LIMIT_STATUS or response.status_code >= 500:
                return _Verdict.RETRYABLE
            return _Verdict.HTTP_ERROR
        # 传输层 200 也要看业务层：金蝶出错时 HTTP 同样是 200
        status = extract_response_status(response.json())
        if status is None:
            return _Verdict.SUCCESS
        if any(kw in status.error_message for kw in _SESSION_LOSS_KEYWORDS):
            return _Verdict.SESSION_INVALID
        return _Verdict.BUSINESS_ERROR

    def _to_result(self, params: BillQueryParams, raw: object) -> BillQueryResult:
        """成功响应 → 类型化结果（列错位等协议破坏由 rows_to_dicts 抛错）。"""
        keys = [k.strip() for k in params.field_keys.split(",")]
        rows = rows_to_dicts(params.field_keys, raw)  # type: ignore[arg-type]
        return BillQueryResult(form_id=params.form_id, field_keys=keys, rows=rows)

    async def _backoff(self, attempt: int, *, response: httpx.Response | None = None) -> None:
        """指数退避 + 抖动；若响应带 ``Retry-After`` 则优先遵从其秒数。"""
        delay = min(_BASE_BACKOFF_SECONDS * (2**attempt), _MAX_BACKOFF_SECONDS)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = float(retry_after)
        await self._sleep(self._jitter(delay))
