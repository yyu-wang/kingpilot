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
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.model_registry import ModelTier


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
    # 取值说明：chat_default（或空）= 自动按 llm_tier 走 ROUTING_CONFIG；
    # 其他逻辑名 = 显式单路由覆盖（忽略档位）
    llm_route: str = Field(default="chat_default", description="MODEL_REGISTRY 的逻辑名")
    # 模型档位（M7-T03）：仅自动路由时生效；T04 预算熔断会据此降档
    llm_tier: ModelTier = Field(default="high", description="模型档位：high / mid / economy")
    llm_api_key: SecretStr = Field(default=SecretStr(""), description="LLM API 密钥（永不入日志）")
    # 默认 0.0（贪心解码）：查询场景要确定性输出；评测（M2-T05）依赖可复现
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="采样温度")

    # ── HTTP API（M9-T01）──
    # 只存 SHA-256 哈希，不存明文 key；格式：
    # {"<64位hash>": {"user_id": "alice", "role": "viewer", "data_scope": {}}}
    api_key_hashes: SecretStr = Field(
        default=SecretStr("{}"),
        description="API Key SHA-256 哈希 → 身份映射（JSON）",
    )

    # ── Prompt 版本管理（M8-T04）──
    prompts_dir: Path = Field(
        default=Path(__file__).resolve().parent.parent / "prompts",
        description="Prompt 版本目录（含 rollout.json）",
    )

    # ── 成本治理（M7-T03/T04）──
    llm_budget_usd: float = Field(
        default=5.0, gt=0, description="进程级 LLM 预算（USD），超限自动降档"
    )
    llm_budget_downgrade_ratio: float = Field(
        default=0.8, gt=0.0, lt=1.0, description="预算占用率达到此比例触发首次降档"
    )
    semantic_cache_ttl_seconds: int = Field(
        default=300, gt=0, description="只读计划缓存有效期（秒）"
    )
    semantic_cache_max_entries: int = Field(
        default=128, ge=1, description="只读计划缓存最大条数（LRU 淘汰）"
    )

    # ── 在线抽样（M8-T02）──
    online_sample_rate: float = Field(
        default=0.0, ge=0.0, le=1.0, description="在线样本采样率（0=关闭；生产建议 0.05）"
    )
    online_sample_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "samples",
        description="在线样本目录（按天一个 JSONL 文件）",
    )
    online_sample_max_per_day: int = Field(
        default=500, ge=1, description="在线样本每日上限（防磁盘失控）"
    )

    # ── Agent 主循环（M3-T01）──
    # 多步编排（reflect 续跑）的轮次上限：单轮=1，默认 5 轮；
    # 防 reflect 死循环耗尽 LLM 调用预算（模块规范 4：循环必须有上限且进配置）
    agent_max_steps: int = Field(default=5, ge=1, description="主循环最大多步编排轮次")

    # ── 记忆压缩（M3-T03，方案 §5.3 三触发条件原值）──
    memory_token_budget: int = Field(
        default=12000, gt=0, description="三层记忆送 LLM 的总 token 预算（紧急压缩线）"
    )
    memory_warning_ratio: float = Field(
        default=0.7, gt=0.0, le=1.0, description="预算占用率超过此值触发积极压缩"
    )
    memory_raw_keep_rounds: int = Field(
        default=6, ge=1, description="常规滚动压缩时 Layer 1 保留的最近对话轮数"
    )

    # ── 交互入口（M3-T07）──
    session_store_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "sessions",
        description="本地会话持久化目录（每个会话一个 JSON 文件）",
    )

    # ── 幂等层（M5-T01）──
    idempotency_ttl_seconds: int = Field(
        default=86_400, gt=0, description="幂等 Key 去重窗口（秒，默认 24 小时）"
    )

    # ── 写操作账本（M5-T03）──
    ledger_db_path: Path = Field(
        default=Path.home() / ".kingpilot" / "ledger.sqlite3",
        description="写操作溯源账本 SQLite 文件路径（append-only）",
    )

    # ── 全链路 Trace（M6-T03）──
    trace_store_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "traces",
        description="Trace 落盘目录（每个 trace 一个 JSON 文件）",
    )

    # ── 审计事件（M6-T04）──
    audit_log_path: Path = Field(
        default=Path.home() / ".kingpilot" / "audit.jsonl",
        description="审计事件 JSONL 文件（append-only）",
    )

    # ── 死信队列（M6-T06）──
    dead_letter_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "dead_letters",
        description="死信目录（每条失败事务一个 JSON 文件）",
    )

    # ── 审批中心（M9-T04）──
    approval_store_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "approvals",
        description="审批单目录（一单一个 JSON，0600）",
    )
    approval_ttl_seconds: int = Field(default=1800, gt=0, description="待审批动作有效期（秒）")

    # ── 运营看板（M9-T06）──
    dashboard_timezone: str = Field(
        default="Asia/Shanghai", description="看板切日时区（IANA 名称）"
    )

    # ── Badcase 处理台（M9-T07）──
    badcase_store_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "badcases",
        description="Badcase 目录（一单一个 JSON，0600）",
    )
    eval_dataset_dir: Path = Field(
        default=Path(__file__).resolve().parent.parent / "evals" / "datasets",
        description="版本化评测集根目录",
    )

    # ── RAG 知识库（M9-T08）──
    rag_source_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "knowledge",
        description="知识文档源目录（支持 md/txt，按一级目录名分级）",
    )
    rag_index_dir: Path = Field(
        default=Path.home() / ".kingpilot" / "rag_index",
        description="RAG 索引目录（index.json）",
    )

    # ── 密钥来源（M9-T10）──
    secret_provider: Literal["env", "file", "vault"] = Field(
        default="env", description="密钥来源：env / file / vault"
    )
    secret_cache_ttl_seconds: int = Field(
        default=60, ge=1, description="文件/Vault 密钥缓存 TTL（秒）"
    )
    secret_file_path: Path = Field(
        default=Path.home() / ".kingpilot" / "secrets.json",
        description="文件密钥路径（JSON，权限必须 0600）",
    )
    vault_url: str = Field(default="", description="Vault 地址，如 https://vault.example.com")
    vault_token: SecretStr = Field(default=SecretStr(""), description="Vault Token（永不入日志）")
    vault_mount: str = Field(default="secret", description="Vault KV v2 mount")
    vault_path: str = Field(default="kingpilot", description="Vault KV v2 path")
    vault_timeout_seconds: float = Field(default=3.0, gt=0, description="Vault 请求超时（秒）")
    vault_allow_fallback: bool = Field(
        default=False, description="Vault 不可用时是否回退环境变量（默认失败）"
    )

    @property
    def is_prod(self) -> bool:
        """是否生产环境（用于后续分支：是否允许真实写操作等）。"""
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    """进程内单例。测试用 ``get_settings.cache_clear()`` 复位。"""
    return Settings()
