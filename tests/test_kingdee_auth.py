"""M1-T03：KingdeeAuth 单测（respx 零网络）。

锚点：签名固定向量由官方算法独立计算（Python hashlib 离线算好后硬编码），
防止实现与断言同义反复。
"""

from collections.abc import Callable

import httpx
import pytest
import respx

from config.settings import Settings
from infrastructure.exceptions import KingdeeAuthError
from kingdee.auth import KingdeeAuth

_SERVER = "https://test.ik3cloud.com"
_LOGIN_URL = (
    f"{_SERVER}/K3Cloud/Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginBySign.common.kdsvc"
)
_ACCT_ID = "66583a5b4c621c"  # pragma: allowlist secret（官方文档示例账套ID，非真凭证）
_SECRET = "super_secret_value_12345"  # pragma: allowlist secret（测试假值，非真凭证）
# 固定向量：acct=66583a5b4c621c user=demo app_id=test_app_id secret=test_app_secret ts=1723791948
# 注：该用例用独立凭证组，见 test_build_sign_known_answer 内构造
_KNOWN_SIGN = "9f7aecb22e1224ab5f24a36c1d2858b9cc4be72aedff20eaaf780190dfc589ec"  # pragma: allowlist secret（由假值算出的测试向量）


def _settings() -> Settings:
    return Settings(
        kingdee_server_url=_SERVER,
        kingdee_acct_id=_ACCT_ID,
        kingdee_user="demo",
        kingdee_app_id="test_app_id",
        kingdee_app_secret=_SECRET,
    )


def _make_auth(
    clock: Callable[[], float] | None = None, ttl: float = 1500.0
) -> tuple[KingdeeAuth, httpx.AsyncClient]:
    http = httpx.AsyncClient()
    auth = KingdeeAuth(_settings(), http, clock=clock or (lambda: 1_000_000.0), session_ttl=ttl)
    return auth, http


# ── 签名算法 ──


def test_build_sign_known_answer() -> None:
    """固定向量锚定官方算法：排序(Ordinal 等价) + SHA256 + hex 小写。"""
    settings = Settings(
        kingdee_server_url=_SERVER,
        kingdee_acct_id=_ACCT_ID,
        kingdee_user="demo",
        kingdee_app_id="test_app_id",
        kingdee_app_secret="test_app_secret",  # pragma: allowlist secret（测试假值）
    )
    auth = KingdeeAuth(settings, httpx.AsyncClient())
    assert auth._build_sign(1723791948) == _KNOWN_SIGN


def test_build_sign_depends_on_timestamp() -> None:
    """时间戳进签名：不同时间戳签名不同（防重放的基本性质）。"""
    auth, _ = _make_auth()
    assert auth._build_sign(1000) != auth._build_sign(1001)


# ── 登录成功 / 失败 ──


@respx.mock
async def test_login_success_marks_session_valid() -> None:
    route = respx.post(_LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"LoginResultType": 1, "Context": {}})
    )
    auth, _ = _make_auth()
    await auth.ensure_session()
    assert route.call_count == 1
    # 报文锚点：parameters 顺序 = [账套, 用户, 应用ID, 时间戳, 签名, lcId]
    sent = route.calls[0].request.read()
    assert f'"{_ACCT_ID}"'.encode() in sent and b"2052" in sent
    assert _SECRET.encode() not in sent  # 密钥永不出网


@respx.mock
async def test_login_rejected_raises_without_leaking_secret() -> None:
    """LoginResultType=-1 → KingdeeAuthError；异常消息不含密钥与签名（脱敏红线）。"""
    respx.post(_LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"LoginResultType": -1, "Message": "验证签名失败"})
    )
    auth, _ = _make_auth()
    with pytest.raises(KingdeeAuthError, match="LoginResultType=-1"):
        await auth.ensure_session()
    try:
        await auth.ensure_session()
    except KingdeeAuthError as exc:
        text = str(exc)
        assert _SECRET not in text
        assert _KNOWN_SIGN not in text


@respx.mock
async def test_login_http_500_raises_auth_error() -> None:
    respx.post(_LOGIN_URL).mock(return_value=httpx.Response(500))
    auth, _ = _make_auth()
    with pytest.raises(KingdeeAuthError):
        await auth.ensure_session()


@respx.mock
async def test_login_timeout_raises_auth_error() -> None:
    respx.post(_LOGIN_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    auth, _ = _make_auth()
    with pytest.raises(KingdeeAuthError):
        await auth.ensure_session()


async def test_login_missing_credentials_raises_before_request() -> None:
    """凭证缺失：本地 fail-closed，不发起任何网络请求。"""
    settings = Settings()  # 全部默认空
    auth = KingdeeAuth(settings, httpx.AsyncClient())
    with pytest.raises(KingdeeAuthError, match="凭证缺失"):
        await auth.ensure_session()


# ── 缓存与过期刷新 ──


@respx.mock
async def test_session_cached_within_ttl() -> None:
    route = respx.post(_LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"LoginResultType": 1})
    )
    auth, _ = _make_auth()
    await auth.ensure_session()
    await auth.ensure_session()
    assert route.call_count == 1  # 未过期不重复登录


@respx.mock
async def test_session_refreshes_after_ttl() -> None:
    route = respx.post(_LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"LoginResultType": 1})
    )
    now = [1_000_000.0]
    auth, _ = _make_auth(clock=lambda: now[0])
    await auth.ensure_session()
    now[0] += 1501.0  # 假钟推过 TTL（注入 clock 的价值：零 sleep 测时间）
    await auth.ensure_session()
    assert route.call_count == 2


@respx.mock
async def test_refresh_failure_raises_auth_error() -> None:
    """任务卡验收路径：过期刷新失败 → KingdeeAuthError。"""
    route = respx.post(_LOGIN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"LoginResultType": 1}),
            httpx.Response(200, json={"LoginResultType": -1, "Message": "验证签名失败"}),
        ]
    )
    now = [1_000_000.0]
    auth, _ = _make_auth(clock=lambda: now[0])
    await auth.ensure_session()
    now[0] += 1501.0
    with pytest.raises(KingdeeAuthError):
        await auth.ensure_session()
    assert route.call_count == 2


# ── 并发刷新防抖 ──


@respx.mock
async def test_concurrent_ensure_session_logs_in_once() -> None:
    import asyncio

    route = respx.post(_LOGIN_URL).mock(
        return_value=httpx.Response(200, json={"LoginResultType": 1})
    )
    auth, _ = _make_auth()
    await asyncio.gather(*[auth.ensure_session() for _ in range(10)])
    assert route.call_count == 1  # double-checked locking：排队协程进锁后命中快路径


@respx.mock
async def test_queued_coroutines_hit_inlock_cached_path() -> None:
    """锁内二次检查命中（auth.py L67）：真实并发窗口下排队协程直接复用会话。

    Why 需要可挂起的登录：respx 即时返回会让首协程一口气完成登录，
    其余协程全走锁外快路径（L64），排队路径从不执行——上面的防抖用例
    因此漏掉 L67 分支（M1-T07 覆盖率自查发现）。``sleep(0)`` 精确让出一次
    控制权：其余协程在登录完成前完成锁外检查、进锁排队，登录完成后
    它们的锁内二次检查命中已缓存会话。
    """
    import asyncio

    async def slow_login(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)  # 持锁挂起：制造「其余协程排队」的真实窗口
        return httpx.Response(200, json={"LoginResultType": 1})

    route = respx.post(_LOGIN_URL).mock(side_effect=slow_login)
    auth, _ = _make_auth()
    await asyncio.gather(*[auth.ensure_session() for _ in range(10)])
    assert route.call_count == 1  # 排队的 9 个协程必须复用首协程建立的会话
