"""Settings 配置加载单测（M0-T04 验收）。

覆盖：默认值 / env 覆盖 / 枚举边界 / 未知字段拒绝 / 单例 / 密钥脱敏。
注意：所有用例通过 init 入参构造并禁用 .env 读取，避免本机文件污染。
"""

import pytest
from pydantic import ValidationError

from config.settings import Settings, get_settings


def _make(**overrides: object) -> Settings:
    """构造一个不读 .env、不看外部环境变量的 Settings。"""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestDefaults:
    """正常路径：无任何输入时的默认值。"""

    def test_env_default_is_dev(self) -> None:
        assert _make().env == "dev"

    def test_log_level_default_is_info(self) -> None:
        assert _make().log_level == "INFO"

    def test_kingdee_tuning_defaults(self) -> None:
        s = _make()
        assert s.kingdee_timeout_seconds == 10.0
        assert s.kingdee_max_concurrency == 5
        assert s.kingdee_max_retries == 3

    def test_kingdee_credentials_default_empty(self) -> None:
        s = _make()
        assert s.kingdee_server_url == ""
        assert s.kingdee_app_secret.get_secret_value() == ""


class TestEnvOverride:
    """加载：环境变量正确映射到字段。"""

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KINGPILOT_ENV", "prod")
        monkeypatch.setenv("KINGPILOT_KINGDEE_APP_ID", "sandbox-app-id")
        s = Settings(_env_file=None)
        assert s.env == "prod"
        assert s.is_prod is True
        assert s.kingdee_app_id == "sandbox-app-id"

    def test_init_arg_beats_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 优先级：init 入参 > 环境变量
        monkeypatch.setenv("KINGPILOT_ENV", "prod")
        s = _make(env="test")
        assert s.env == "test"


class TestBoundary:
    """边界：枚举闭集校验。"""

    def test_env_legal_values_pass(self) -> None:
        for v in ("dev", "test", "prod"):
            assert _make(env=v).env == v

    def test_env_illegal_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(
                env="staging"
            )  # 运行时校验：Literal 约束由 pydantic 在 init 时检查，mypy 无法感知 kwargs 内容


class TestUnknownField:
    """验收点：未知 init 入参被拒（extra="forbid"）。"""

    def test_unknown_init_arg_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(
                unknown_field="x"
            )  # 运行时校验：extra="forbid" 由 pydantic 检查，mypy 无法感知 kwargs 内容

    def test_unknown_env_var_silently_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 固化 pydantic-settings 原生行为：未知环境变量不报错、被忽略
        monkeypatch.setenv("KINGPILOT_TOTALLY_UNKNOWN", "1")
        s = Settings(_env_file=None)
        assert not hasattr(s, "totally_unknown")


class TestSingleton:
    """get_settings() 进程内单例。"""

    def test_singleton_identity(self) -> None:
        assert get_settings() is get_settings()

    def test_cache_clear_resets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = get_settings()
        monkeypatch.setenv("KINGPILOT_ENV", "test")
        get_settings.cache_clear()
        second = get_settings()
        assert first is not second
        assert second.env == "test"


class TestSecretMasking:
    """密钥红线：明文不出现在 repr / str。"""

    def test_secret_not_in_repr(self) -> None:
        s = _make(kingdee_app_secret="super-secret-value")
        assert "super-secret-value" not in repr(s)
        assert "super-secret-value" not in str(s)

    def test_secret_value_accessible_explicitly(self) -> None:
        s = _make(kingdee_app_secret="super-secret-value")
        assert s.kingdee_app_secret.get_secret_value() == "super-secret-value"
