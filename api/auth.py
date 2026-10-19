"""API Key 目录（M9-T01）。

安全设计：
- 配置只存 SHA-256 哈希，仓库/日志/环境里都不出现明文 key
- 认证按哈希常量时间比较，不按位置提前返回
- 配置非法在启动期抛 ``ApiKeyConfigError``（fail-closed）

本模块只负责「哈希 → 身份」；FastAPI 依赖注入在 ``api.deps``，
避免 ``auth ↔ deps`` 循环导入。
"""

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field, ValidationError

from config.settings import Settings
from infrastructure.exceptions import ApiKeyConfigError
from models.base import KingPilotBaseModel, Role
from models.tenant import TENANT_ID_PATTERN

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# 与 SessionStore 的命名规则一致：user_id 会作为目录名，必须挡住 ``../``
_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ApiKeyIdentity(KingPilotBaseModel):
    """一条 API Key 对应的身份与权限上下文。"""

    user_id: str = Field(min_length=1, max_length=64, pattern=_USER_ID_RE.pattern)
    tenant_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=TENANT_ID_PATTERN,
        description="租户 ID（M9-T11 必填；缺失启动失败）",
    )
    role: Role
    data_scope: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ApiKeyDirectory:
    """哈希 → 身份目录；启动期构建一次，运行时只读。"""

    _records: Mapping[str, ApiKeyIdentity]

    @classmethod
    def from_settings(cls, settings: Settings) -> "ApiKeyDirectory":
        """解析 ``Settings.api_key_hashes``；任何结构问题都启动失败。"""
        raw = settings.api_key_hashes.get_secret_value().strip()
        if not raw:
            return cls({})
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise ApiKeyConfigError("API Key 配置不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ApiKeyConfigError("API Key 配置必须是 JSON 对象")

        records: dict[str, ApiKeyIdentity] = {}
        for index, (key_hash, value) in enumerate(payload.items()):
            if not isinstance(key_hash, str) or not _HASH_RE.fullmatch(key_hash):
                raise ApiKeyConfigError(f"第 {index} 条 API Key 哈希格式非法")
            try:
                records[key_hash] = ApiKeyIdentity.model_validate(value)
            except ValidationError as exc:
                raise ApiKeyConfigError(f"第 {index} 条 API Key 身份配置非法") from exc
        return cls(records)

    def authenticate(self, presented: str) -> ApiKeyIdentity | None:
        """按 SHA-256 哈希匹配；遍历全部条目，避免按位置泄漏匹配信息。"""
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        matched: ApiKeyIdentity | None = None
        for key_hash, identity in self._records.items():
            if hmac.compare_digest(digest, key_hash):
                matched = identity
        return matched
