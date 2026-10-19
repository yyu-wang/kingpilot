"""密钥统一入口（M9-T10）：Env / File / Vault + 轮换 + fail-closed。

设计边界：
- **只读**：Provider 只负责取密钥，不创建/写入 Vault 或文件
- **零泄漏**：异常消息、日志、``repr`` 只含逻辑名/来源/错误码，不含密钥值
- **可轮换**：文件/Vault 按 TTL 自动刷新，``refresh()`` 强制刷新；
  env 模式变更需重启（进程内环境变量不可变）
- **fail-closed**：Vault 不可用默认直接报错；显式开启后才回退 env

Vault 只支持 KV v2：``GET /v1/{mount}/data/{path}``，令牌走 ``X-Vault-Token``。
"""

import json
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
from pydantic import SecretStr

from config.settings import Settings
from infrastructure.exceptions import SecretError

# 文件权限：除属主外任何权限都视为过宽
_UNSAFE_FILE_MODE = 0o077


class SecretName(StrEnum):
    """项目内允许读取的密钥逻辑名（闭集，防止任意键探测）。"""

    LLM_API_KEY = "llm_api_key"
    KINGDEE_APP_SECRET = "kingdee_app_secret"


class SecretProvider(ABC):
    """密钥读取抽象：同步接口，缓存与轮换由实现负责。"""

    @property
    @abstractmethod
    def source(self) -> str:
        """来源标识（env/file/vault，用于日志，不含任何密钥）。"""

    @abstractmethod
    def _load(self, name: SecretName) -> SecretStr:
        """从底层来源读取；失败抛 ``SecretError``。"""

    @abstractmethod
    def refresh(self) -> None:
        """强制失效缓存（下次读取重新拉取）。"""

    def get(self, name: SecretName) -> SecretStr:
        """读取密钥（可能为空，由调用方决定是否必需）。"""
        return self._load(name)

    def get_required(self, name: SecretName) -> SecretStr:
        """读取非空密钥；空值 fail-closed。"""
        value = self.get(name)
        if not value.get_secret_value().strip():
            raise SecretError("密钥未配置", code="secret_missing", secret=name.value)
        return value


class EnvSecretProvider(SecretProvider):
    """环境变量来源：经 Settings 读取，不直接访问 ``os.environ``。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def source(self) -> str:
        return "env"

    def _load(self, name: SecretName) -> SecretStr:
        if name is SecretName.LLM_API_KEY:
            return self._settings.llm_api_key
        if name is SecretName.KINGDEE_APP_SECRET:
            return self._settings.kingdee_app_secret
        raise SecretError("未知密钥逻辑名", code="secret_unknown", secret=str(name))

    def refresh(self) -> None:
        """env 模式无缓存；进程内环境变量不可变，轮换需重启。"""

    def __repr__(self) -> str:
        return "EnvSecretProvider()"


class FileSecretProvider(SecretProvider):
    """JSON 文件来源：权限必须 0600，按 TTL 缓存。"""

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise SecretError("密钥缓存 TTL 必须大于 0", code="secret_config")
        self._path = path
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, SecretStr] | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def source(self) -> str:
        return "file"

    def _load(self, name: SecretName) -> SecretStr:
        payload = self._payload()
        value = payload.get(name.value)
        if value is None:
            raise SecretError("密钥未配置", code="secret_missing", secret=name.value)
        return value

    def _payload(self) -> dict[str, SecretStr]:
        with self._lock:
            now = self._clock()
            if self._cache is not None and now < self._expires_at:
                return self._cache
            payload = self._read()
            self._cache = payload
            self._expires_at = now + self._ttl
            return payload

    def _read(self) -> dict[str, SecretStr]:
        try:
            stat = self._path.stat()
        except OSError as exc:
            raise SecretError("密钥文件不可用", code="secret_unavailable") from exc
        if stat.st_mode & _UNSAFE_FILE_MODE:
            raise SecretError(
                "密钥文件权限过宽，请执行 chmod 600",
                code="secret_file_permissions",
            )
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SecretError("密钥文件不可用", code="secret_unavailable") from exc
        except ValueError as exc:
            raise SecretError("密钥文件不是合法 JSON", code="secret_invalid") from exc
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise SecretError("密钥文件结构非法", code="secret_invalid")
        return {key: SecretStr(value) for key, value in raw.items()}

    def refresh(self) -> None:
        with self._lock:
            self._cache = None
            self._expires_at = 0.0

    def __repr__(self) -> str:
        return f"FileSecretProvider(path={self._path})"


class VaultSecretProvider(SecretProvider):
    """Vault KV v2 来源：短超时 + TTL 缓存 + 默认 fail-closed。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: SecretStr,
        mount: str,
        path: str,
        timeout_seconds: float,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not base_url.strip() or not token.get_secret_value().strip():
            raise SecretError("Vault 地址或 Token 未配置", code="secret_config")
        if not mount.strip() or not path.strip():
            raise SecretError("Vault mount/path 未配置", code="secret_config")
        if ttl_seconds <= 0:
            raise SecretError("密钥缓存 TTL 必须大于 0", code="secret_config")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._mount = mount.strip("/")
        self._path = path.strip("/")
        self._timeout = timeout_seconds
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, SecretStr] | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def source(self) -> str:
        return "vault"

    def _load(self, name: SecretName) -> SecretStr:
        payload = self._payload()
        value = payload.get(name.value)
        if value is None:
            raise SecretError("密钥未配置", code="secret_missing", secret=name.value)
        return value

    def _payload(self) -> dict[str, SecretStr]:
        with self._lock:
            now = self._clock()
            if self._cache is not None and now < self._expires_at:
                return self._cache
            payload = self._fetch()
            self._cache = payload
            self._expires_at = now + self._ttl
            return payload

    def _fetch(self) -> dict[str, SecretStr]:
        url = (
            f"{self._base_url}/v1/{quote(self._mount, safe='')}/data/{quote(self._path, safe='/')}"
        )
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.get(
                    url,
                    headers={"X-Vault-Token": self._token.get_secret_value()},
                )
        except httpx.HTTPError as exc:
            raise SecretError("Vault 不可用", code="secret_unavailable") from exc

        if response.status_code in {403, 404}:
            # 不区分「无权限」与「不存在」，避免路径探测
            raise SecretError("Vault 密钥不可读", code="secret_unavailable")
        if response.status_code != 200:
            raise SecretError(f"Vault 返回 HTTP {response.status_code}", code="secret_unavailable")

        try:
            body: Any = response.json()
            data = body["data"]["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SecretError("Vault 响应结构不符", code="secret_invalid") from exc
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise SecretError("Vault 响应结构不符", code="secret_invalid")
        return {key: SecretStr(value) for key, value in data.items()}

    def refresh(self) -> None:
        with self._lock:
            self._cache = None
            self._expires_at = 0.0

    def __repr__(self) -> str:
        return f"VaultSecretProvider(url={self._base_url}, mount={self._mount}, path={self._path})"


class FallbackSecretProvider(SecretProvider):
    """主源不可用时回退备用源；只在 ``secret_unavailable`` 时回退。"""

    def __init__(self, primary: SecretProvider, fallback: SecretProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def source(self) -> str:
        return f"{self._primary.source}->{self._fallback.source}"

    def _load(self, name: SecretName) -> SecretStr:
        try:
            return self._primary.get(name)
        except SecretError as exc:
            # 只对「不可用」回退；缺失/结构错误属于配置问题，必须暴露
            if exc.code != "secret_unavailable":
                raise
            logger.warning(
                "密钥主源不可用，回退备用源 | secret={} code={} primary={}",
                name.value,
                exc.code,
                self._primary.source,
            )
            return self._fallback.get(name)

    def refresh(self) -> None:
        self._primary.refresh()
        self._fallback.refresh()

    def __repr__(self) -> str:
        return f"FallbackSecretProvider(primary={self._primary!r}, fallback={self._fallback!r})"


def build_secret_provider(settings: Settings) -> SecretProvider:
    """按 ``Settings.secret_provider`` 装配；Vault 缺配置 fail-closed。"""
    mode = settings.secret_provider
    if mode == "env":
        return EnvSecretProvider(settings)
    if mode == "file":
        return FileSecretProvider(
            settings.secret_file_path,
            ttl_seconds=settings.secret_cache_ttl_seconds,
        )
    if mode == "vault":
        vault = VaultSecretProvider(
            base_url=settings.vault_url,
            token=settings.vault_token,
            mount=settings.vault_mount,
            path=settings.vault_path,
            timeout_seconds=settings.vault_timeout_seconds,
            ttl_seconds=settings.secret_cache_ttl_seconds,
        )
        if settings.vault_allow_fallback:
            return FallbackSecretProvider(vault, EnvSecretProvider(settings))
        return vault
    raise SecretError("未知密钥来源", code="secret_config")
