"""M9-T10：密钥 Provider 单测（Env / File / Vault / 回退 / 零泄漏）。"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from loguru import logger
from pydantic import SecretStr

from config.settings import Settings
from infrastructure.exceptions import SecretError
from infrastructure.secrets import (
    EnvSecretProvider,
    FallbackSecretProvider,
    FileSecretProvider,
    SecretName,
    SecretProvider,
    VaultSecretProvider,
    build_secret_provider,
)
from tests.fakes_vault import VaultServer, start_vault


class _Clock:
    """可推进的假钟（TTL 测试零 sleep）。"""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def _write_secret_file(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)


@pytest.fixture
def vault_server() -> Iterator[VaultServer]:
    server = start_vault()
    try:
        yield server
    finally:
        server.stop()


def _vault(
    server: VaultServer, *, ttl: int = 60, clock: _Clock | None = None
) -> VaultSecretProvider:
    return VaultSecretProvider(
        base_url=server.url,
        token=SecretStr("test-token"),
        mount="secret",
        path="kingpilot",
        timeout_seconds=2.0,
        ttl_seconds=ttl,
        clock=clock or _Clock(),
    )


# ────────────────────────── Env ──────────────────────────


def test_env_provider_reads_settings() -> None:
    settings = _settings(
        llm_api_key=SecretStr("test-key-a"),
        kingdee_app_secret=SecretStr("test-secret-a"),
    )
    provider = EnvSecretProvider(settings)

    assert provider.source == "env"
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"
    assert (
        provider.get_required(SecretName.KINGDEE_APP_SECRET).get_secret_value() == "test-secret-a"
    )


def test_env_provider_missing_raises() -> None:
    provider = EnvSecretProvider(_settings())

    with pytest.raises(SecretError) as exc:
        provider.get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_missing"


def test_env_provider_unknown_name_raises() -> None:
    provider = EnvSecretProvider(_settings())

    with pytest.raises(SecretError) as exc:
        provider.get(cast(SecretName, "unknown"))

    assert exc.value.code == "secret_unknown"


# ────────────────────────── File ──────────────────────────


def test_file_provider_reads_json(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    _write_secret_file(path, {"llm_api_key": "test-key-a"})
    provider = FileSecretProvider(path, ttl_seconds=60)

    assert provider.source == "file"
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"


def test_file_provider_missing_file(tmp_path: Path) -> None:
    provider = FileSecretProvider(tmp_path / "missing.json", ttl_seconds=60)

    with pytest.raises(SecretError) as exc:
        provider.get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_unavailable"


def test_file_provider_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("{broken", encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(SecretError) as exc:
        FileSecretProvider(path, ttl_seconds=60).get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_invalid"


def test_file_provider_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    _write_secret_file(path, {"kingdee_app_secret": "test-secret-a"})

    with pytest.raises(SecretError) as exc:
        FileSecretProvider(path, ttl_seconds=60).get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_missing"


def test_file_provider_rejects_wide_permissions(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    _write_secret_file(path, {"llm_api_key": "test-key-a"}, mode=0o644)

    with pytest.raises(SecretError) as exc:
        FileSecretProvider(path, ttl_seconds=60).get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_file_permissions"


def test_file_provider_caches_until_ttl(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    _write_secret_file(path, {"llm_api_key": "test-key-a"})
    clock = _Clock()
    provider = FileSecretProvider(path, ttl_seconds=60, clock=clock)

    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"
    _write_secret_file(path, {"llm_api_key": "test-key-b"})
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"

    clock.now += 61
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-b"


def test_file_provider_refresh_picks_new_value(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    _write_secret_file(path, {"llm_api_key": "test-key-a"})
    provider = FileSecretProvider(path, ttl_seconds=60)

    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"
    _write_secret_file(path, {"llm_api_key": "test-key-b"})
    provider.refresh()

    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-b"


def test_file_provider_repr_has_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    _write_secret_file(path, {"llm_api_key": "test-key-a"})
    provider = FileSecretProvider(path, ttl_seconds=60)
    provider.get_required(SecretName.LLM_API_KEY)

    assert "test-key-a" not in repr(provider)


# ────────────────────────── Vault ──────────────────────────


def test_vault_provider_reads_kv_v2(vault_server: VaultServer) -> None:
    provider = _vault(vault_server)

    assert provider.source == "vault"
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"


def test_vault_wrong_token_is_unavailable(vault_server: VaultServer) -> None:
    provider = VaultSecretProvider(
        base_url=vault_server.url,
        token=SecretStr("wrong-token"),
        mount="secret",
        path="kingpilot",
        timeout_seconds=2.0,
        ttl_seconds=60,
    )

    with pytest.raises(SecretError) as exc:
        provider.get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_unavailable"
    assert "wrong-token" not in str(exc.value)


def test_vault_http_error_is_unavailable(vault_server: VaultServer) -> None:
    vault_server.state.status = 500
    provider = _vault(vault_server)

    with pytest.raises(SecretError) as exc:
        provider.get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_unavailable"


def test_vault_invalid_payload(vault_server: VaultServer) -> None:
    vault_server.state.payload = {"data": {"data": {"llm_api_key": 123}}}
    provider = _vault(vault_server)

    with pytest.raises(SecretError) as exc:
        provider.get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_invalid"


def test_vault_missing_key(vault_server: VaultServer) -> None:
    vault_server.state.data = {"llm_api_key": "test-key-a"}
    provider = _vault(vault_server)

    with pytest.raises(SecretError) as exc:
        provider.get_required(SecretName.KINGDEE_APP_SECRET)

    assert exc.value.code == "secret_missing"


def test_vault_ttl_and_refresh(vault_server: VaultServer) -> None:
    clock = _Clock()
    provider = _vault(vault_server, ttl=60, clock=clock)
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"

    vault_server.state.data = {"llm_api_key": "test-key-b"}
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-a"

    provider.refresh()
    assert provider.get_required(SecretName.LLM_API_KEY).get_secret_value() == "test-key-b"


def test_vault_repr_has_no_token(vault_server: VaultServer) -> None:
    provider = _vault(vault_server)
    assert "test-token" not in repr(provider)


def test_vault_unavailable_default_fails() -> None:
    provider = VaultSecretProvider(
        base_url="http://127.0.0.1:1",
        token=SecretStr("test-token"),
        mount="secret",
        path="kingpilot",
        timeout_seconds=0.5,
        ttl_seconds=60,
    )

    with pytest.raises(SecretError) as exc:
        provider.get_required(SecretName.LLM_API_KEY)

    assert exc.value.code == "secret_unavailable"


def test_vault_fallback_is_optional_and_logged() -> None:
    settings = _settings(llm_api_key=SecretStr("env-key"))
    primary = VaultSecretProvider(
        base_url="http://127.0.0.1:1",
        token=SecretStr("test-token"),
        mount="secret",
        path="kingpilot",
        timeout_seconds=0.5,
        ttl_seconds=60,
    )
    provider: SecretProvider = FallbackSecretProvider(primary, EnvSecretProvider(settings))
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")

    try:
        value = provider.get_required(SecretName.LLM_API_KEY).get_secret_value()
    finally:
        logger.remove(sink_id)

    assert provider.source == "vault->env"
    assert value == "env-key"
    assert any("回退备用源" in message for message in messages)
    assert "env-key" not in "".join(messages)


# ────────────────────────── 工厂 ──────────────────────────


def test_build_provider_env() -> None:
    settings = _settings(llm_api_key=SecretStr("test-key-a"))
    assert isinstance(build_secret_provider(settings), EnvSecretProvider)


def test_build_provider_file(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    _write_secret_file(path, {"llm_api_key": "test-key-a"})
    settings = _settings(secret_provider="file", secret_file_path=path)

    provider = build_secret_provider(settings)

    assert provider.source == "file"


def test_build_provider_vault_requires_config() -> None:
    settings = _settings(secret_provider="vault")

    with pytest.raises(SecretError) as exc:
        build_secret_provider(settings)

    assert exc.value.code == "secret_config"


def test_build_provider_vault_with_fallback() -> None:
    settings = _settings(
        secret_provider="vault",
        vault_url="http://127.0.0.1:1",
        vault_token=SecretStr("test-token"),
        vault_allow_fallback=True,
    )

    provider = build_secret_provider(settings)

    assert provider.source == "vault->env"
