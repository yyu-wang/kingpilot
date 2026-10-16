"""金蝶 LoginBySign 签名登录与会话生命周期（M1-T03）。

协议要点（官方《WebAPI登录接口介绍》，当前唯一推荐登录方式）：
- 签名：``[账套ID, 用户名, 应用ID, 应用密钥, 时间戳]`` → 字符串序排序 →
  拼接 → SHA256 → hex 小写（PT-146911 之前的旧版本才用 SHA1，本项目不支持）。
- 会话：Cookie 会话（``KDSVCSessionId`` 写入 cookie jar），非 Bearer Token。
- 成功判定：``LoginResultType == 1``（HTTP 同样恒为 200，必须看返回体）。

安全红线：``app_secret`` 只参与本地签名计算，永不出现在报文、日志、异常消息中。
"""

import asyncio
import hashlib
import time
from collections.abc import Callable

import httpx
from loguru import logger

from config.settings import Settings
from infrastructure.exceptions import KingdeeAuthError
from models.kingdee import LoginResult

# 协议固定值（非环境配置，故不放 config/settings）：
_LC_ID_ZH_CN = 2052  # 语言 ID：简体中文（多语言化时再提升为配置）
_LOGIN_PATH = "/K3Cloud/Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginBySign.common.kdsvc"
# INFERRED: 官方未公布会话 TTL（社区反馈约小时级），取保守 25 分钟主动过期；
# 被动检测（收到“会话信息丢失”报错重登）归 T04 client。真环境联调时核对。
_DEFAULT_SESSION_TTL = 25 * 60.0


class KingdeeAuth:
    """LoginBySign 会话生命周期：登录 → 缓存有效期 → 过期重登（并发防抖）。

    会话本体在注入的 ``httpx.AsyncClient`` 的 cookie jar 里（金蝶是 Cookie 会话），
    本类只缓存「有效期元数据」。T04 client 每次调用前 ``await ensure_session()``。

    依赖注入 ``http``（与 client 共享同一连接池/cookie jar）与 ``clock``
    （测试用假钟推进 TTL，零 sleep）。
    """

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        session_ttl: float = _DEFAULT_SESSION_TTL,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clock = clock
        self._session_ttl = session_ttl
        self._expires_at = 0.0  # 0 = 从未登录，必然触发首次登录
        self._refresh_lock = asyncio.Lock()

    async def ensure_session(self) -> None:
        """确保 cookie jar 持有有效会话；过期则重登。

        asyncio 版 double-checked locking：锁外快路径直接返回；锁内再查一次，
        防止 N 个协程排队各刷一次（并发刷新防抖，任务卡验收点）。
        """
        if self._session_valid():
            return
        async with self._refresh_lock:
            if self._session_valid():
                return
            await self._login()

    def _session_valid(self) -> bool:
        """当前时刻会话是否在有效期内。"""
        return self._clock() < self._expires_at

    def invalidate(self) -> None:
        """主动作废会话（下次 ``ensure_session()`` 必然重登）。

        供 client 在检测到「会话信息丢失」类被动失效时调用——
        用公开方法而非跨模块改私有属性，保持封装边界。
        """
        self._expires_at = 0.0
        logger.warning("金蝶会话已主动作废，下次调用将重新登录")

    def _build_sign(self, timestamp: int) -> str:
        """LoginBySign 签名。

        Why ``sorted()``：官方算法是字符串序排序（C# ``StringComparer.Ordinal``），
        Python 默认按 Unicode 码点排序，与其逐字节等价（测试用固定向量锚定）。
        密钥只参与本地计算、从不出网——这是 LoginBySign 相对 LoginByAppSecret
        的本质改进：密钥从「传输凭证」降级为「本地签名盐」，抓包拿不到。
        """
        parts = [
            self._settings.kingdee_acct_id,
            self._settings.kingdee_user,
            self._settings.kingdee_app_id,
            self._settings.kingdee_app_secret.get_secret_value(),
            str(timestamp),
        ]
        digest = hashlib.sha256("".join(sorted(parts)).encode("utf-8")).hexdigest()
        return digest

    async def _login(self) -> None:
        """调 LoginBySign 建立会话。

        Raises:
            KingdeeAuthError: 凭证缺失、传输失败、或 ``LoginResultType != 1``。
                异常消息只含操作名/服务端 Message，永不含密钥与签名（红线）。
        """
        s = self._settings
        missing = [
            name
            for name, value in (
                ("kingdee_server_url", s.kingdee_server_url),
                ("kingdee_acct_id", s.kingdee_acct_id),
                ("kingdee_user", s.kingdee_user),
                ("kingdee_app_id", s.kingdee_app_id),
            )
            if not value
        ]
        if not s.kingdee_app_secret.get_secret_value():
            missing.append("kingdee_app_secret")
        if missing:
            raise KingdeeAuthError(
                f"金蝶凭证缺失: {', '.join(missing)}",
                context={"operation": "LoginBySign", "missing": missing},
            )

        timestamp = int(self._clock())
        payload = {
            "parameters": [
                s.kingdee_acct_id,
                s.kingdee_user,
                s.kingdee_app_id,
                timestamp,
                self._build_sign(timestamp),
                _LC_ID_ZH_CN,
            ]
        }
        url = s.kingdee_server_url.rstrip("/") + _LOGIN_PATH
        try:
            resp = await self._http.post(url, json=payload)
            resp.raise_for_status()
            result = LoginResult.model_validate(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError 覆盖 JSON 解析/模型校验失败；均归一化为认证失败
            raise KingdeeAuthError(
                f"金蝶登录请求失败: {type(exc).__name__}",
                context={"operation": "LoginBySign"},
            ) from exc

        if not result.is_success:
            # 服务端 Message 可能提示失败原因（如验签失败），可携带；密钥/签名永不出现
            raise KingdeeAuthError(
                f"金蝶登录被拒: LoginResultType={result.login_result_type} {result.message or ''}",
                context={"operation": "LoginBySign", "login_result_type": result.login_result_type},
            )

        self._expires_at = self._clock() + self._session_ttl
        logger.info("金蝶会话建立成功，TTL={}s", int(self._session_ttl))
