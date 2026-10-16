"""全项目唯一配置入口（M0-T04）。

规则（全局规范 3.6）：
- 任何模块禁止自行读 ``os.environ``，统一通过 ``get_settings()`` 取值。
- 密钥禁止硬编码 / 入日志，统一走 ``SecretStr``。

加载优先级（pydantic-settings，高 → 低）：
    init 参数 > 环境变量(KINGPILOT_*) > .env 文件 > 字段默认值

注意（M0-T04 实测固化）：
    ``extra="forbid"`` 只对「未知 **init 入参**」报错；
    未知 ``KINGPILOT_*`` 环境变量会被**静默忽略**（pydantic-settings 原生行为）。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程级配置。

    - 运行时环境分层：dev / test / prod
    - 金蝶凭证块：M1-T01 填沙箱；生产走真实环境变量，不进仓库
    """

    model_config = SettingsConfigDict(
        env_prefix="KINGPILOT_",  # 命名空间隔离，避免与其他进程变量串味
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",  # 未知 init 入参直接拒绝（fail-closed）
        case_sensitive=False,
    )

    # ── 运行时环境分层 ──
    env: Literal["dev", "test", "prod"] = Field(default="dev")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")

    # ── 金蝶云星空（沙箱 / 生产）──
    kingdee_server_url: str = Field(default="", description="如 https://xxx.ik3cloud.com")
    kingdee_acct_id: str = Field(default="", description="账套 ID")
    kingdee_user: str = Field(default="", description="登录用户名")
    kingdee_app_id: str = Field(default="", description="应用 ID")
    kingdee_app_secret: SecretStr = Field(
        default=SecretStr(""), description="应用密钥（永不入日志）"
    )

    # ── 金蝶客户端调优参数 ──
    kingdee_timeout_seconds: float = Field(default=10.0, gt=0)
    kingdee_max_concurrency: int = Field(default=5, ge=1)
    kingdee_max_retries: int = Field(default=3, ge=0)

    # ── LLM（M2-T02：DeepSeek，OpenAI 兼容协议）──
    # 路由表在 config/model_registry.py（规则与逻辑分离）；密钥只进 Settings
    llm_route: str = Field(default="chat_default", description="MODEL_REGISTRY 的逻辑名")
    llm_api_key: SecretStr = Field(default=SecretStr(""), description="LLM API 密钥（永不入日志）")

    @property
    def is_prod(self) -> bool:
        """是否生产环境（用于后续分支：是否允许真实写操作等）。"""
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试用 ``get_settings.cache_clear()`` 复位。"""
    return Settings()
