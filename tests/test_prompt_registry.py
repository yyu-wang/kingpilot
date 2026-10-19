"""M8-T04：Prompt 版本管理单测。

覆盖配置加载校验、灰度确定性/分布、回滚持久化，
以及 Provider 注入 Registry 后的实际请求文本。
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from agent.providers import OpenAICompatibleProvider
from config.settings import Settings
from governance.prompt_registry import PromptRegistry
from infrastructure.exceptions import PromptRegistryError
from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
)

_API_KEY = "test-key-not-a-real-credential"  # pragma: allowlist secret
_URL = "https://api.deepseek.com/chat/completions"

_PROMPTS = {
    "reflect": {"v1": "REFLECT-V1", "v2": "REFLECT-V2"},
    "generate": {"v1": "GENERATE-V1"},
    "summarize": {"v1": "SUMMARIZE-V1"},
}


def _config(rollout: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    return rollout or {
        "reflect": {"active": "v1", "rollout": {}},
        "generate": {"active": "v1", "rollout": {}},
        "summarize": {"active": "v1", "rollout": {}},
    }


def _write_root(
    tmp_path: Path,
    *,
    prompts: dict[str, dict[str, str]] | None = None,
    config: dict[str, Any] | None = None,
) -> Path:
    root = tmp_path / "prompts"
    for prompt_id, versions in (prompts or _PROMPTS).items():
        directory = root / prompt_id
        directory.mkdir(parents=True, exist_ok=True)
        for version, text in versions.items():
            (directory / f"{version}.txt").write_text(text, encoding="utf-8")
    (root / "rollout.json").write_text(
        json.dumps(config or _config(), ensure_ascii=False), encoding="utf-8"
    )
    return root


def _registry(tmp_path: Path, **kwargs: Any) -> PromptRegistry:
    return PromptRegistry(_write_root(tmp_path, **kwargs))


# ── 加载与校验 ──


def test_get_active_version(tmp_path: Path) -> None:
    version = _registry(tmp_path).get("reflect")
    assert version.prompt_id == "reflect"
    assert version.version == "v1"
    assert version.text == "REFLECT-V1"


def test_missing_config_fails(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    (root / "rollout.json").unlink()
    with pytest.raises(PromptRegistryError, match="配置不存在"):
        PromptRegistry(root)


def test_bad_config_json_fails(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    (root / "rollout.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="合法 JSON"):
        PromptRegistry(root)


def test_missing_active_file_fails(tmp_path: Path) -> None:
    config = _config()
    config["reflect"]["active"] = "v9"
    with pytest.raises(PromptRegistryError, match="版本文件不存在"):
        _registry(tmp_path, config=config)


def test_invalid_rollout_ratio_fails(tmp_path: Path) -> None:
    config = _config()
    config["reflect"]["rollout"] = {"v2": 1.5}
    with pytest.raises(PromptRegistryError, match="灰度比例"):
        _registry(tmp_path, config=config)


def test_rollout_sum_over_one_fails(tmp_path: Path) -> None:
    prompts = {"reflect": {"v1": "V1", "v2": "V2", "v3": "V3"}}
    config = {
        "reflect": {"active": "v1", "rollout": {"v2": 0.6, "v3": 0.6}},
    }
    with pytest.raises(PromptRegistryError, match="灰度比例之和"):
        _registry(tmp_path, prompts=prompts, config=config)


def test_empty_version_file_fails(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    (root / "reflect" / "v1.txt").write_text("   ", encoding="utf-8")
    with pytest.raises(PromptRegistryError, match="版本文件为空"):
        PromptRegistry(root)


def test_invalid_version_name_rejected(tmp_path: Path) -> None:
    config = {"reflect": {"active": "../evil", "rollout": {}}}
    with pytest.raises(PromptRegistryError, match="非法 Prompt 版本"):
        _registry(tmp_path, config=config)


def test_unknown_prompt_fails(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="未知 Prompt"):
        _registry(tmp_path).get("nope")


def test_path_traversal_names_rejected(tmp_path: Path) -> None:
    config = _config()
    config["../evil"] = {"active": "v1", "rollout": {}}
    with pytest.raises(PromptRegistryError, match="非法 Prompt 名称"):
        _registry(tmp_path, config=config)


def test_missing_root_fails(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="目录不存在"):
        PromptRegistry(tmp_path / "nope")


def test_root_property(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    assert PromptRegistry(root).root == root


def test_list_versions_without_directory(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    registry = PromptRegistry(root)
    for path in (root / "generate").iterdir():
        path.unlink()
    (root / "generate").rmdir()

    assert registry.list_versions("generate") == ()


def _registry_with_raw_config(tmp_path: Path, payload: Any) -> None:
    root = _write_root(tmp_path)
    (root / "rollout.json").write_text(json.dumps(payload), encoding="utf-8")
    PromptRegistry(root)


def test_config_must_be_non_empty_object(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="非空对象"):
        _registry_with_raw_config(tmp_path, [])


def test_prompt_config_must_be_object(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="配置必须是对象"):
        _registry_with_raw_config(tmp_path, {"reflect": "bad"})


def test_active_must_be_string(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="缺少 active"):
        _registry_with_raw_config(tmp_path, {"reflect": {"active": 123}})


def test_rollout_must_be_object(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="rollout 必须是对象"):
        _registry_with_raw_config(tmp_path, {"reflect": {"active": "v1", "rollout": "bad"}})


def test_rollout_ratio_must_be_number(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="必须是数值"):
        _registry_with_raw_config(tmp_path, {"reflect": {"active": "v1", "rollout": {"v2": True}}})


def test_set_rollout_sum_over_one_fails(tmp_path: Path) -> None:
    prompts = {"reflect": {"v1": "V1", "v2": "V2", "v3": "V3"}}
    config = {"reflect": {"active": "v1", "rollout": {}}}
    root = _write_root(tmp_path, prompts=prompts, config=config)
    registry = PromptRegistry(root)

    registry.set_rollout("reflect", "v2", 0.7)
    with pytest.raises(PromptRegistryError, match="比例之和"):
        registry.set_rollout("reflect", "v3", 0.5)


# ── 灰度选择 ──


def test_empty_key_always_active(tmp_path: Path) -> None:
    config = _config()
    config["reflect"]["rollout"] = {"v2": 0.9}
    registry = _registry(tmp_path, config=config)

    assert registry.get("reflect").version == "v1"


def test_same_key_is_stable(tmp_path: Path) -> None:
    config = _config()
    config["reflect"]["rollout"] = {"v2": 0.5}
    registry = _registry(tmp_path, config=config)

    versions = {registry.get("reflect", rollout_key="session-42").version for _ in range(20)}

    assert len(versions) == 1


def test_half_rollout_hits_both_versions(tmp_path: Path) -> None:
    config = _config()
    config["reflect"]["rollout"] = {"v2": 0.5}
    registry = _registry(tmp_path, config=config)

    versions = {registry.get("reflect", rollout_key=f"session-{i}").version for i in range(200)}

    assert versions == {"v1", "v2"}


def test_list_versions_sorted(tmp_path: Path) -> None:
    assert _registry(tmp_path).list_versions("reflect") == ("v1", "v2")


# ── 灰度设置与回滚 ──


def test_set_rollout_persists(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    registry = PromptRegistry(root)

    registry.set_rollout("reflect", "v2", 0.3)

    reloaded = PromptRegistry(root)
    versions = {reloaded.get("reflect", rollout_key=f"session-{i}").version for i in range(200)}
    assert versions == {"v1", "v2"}


def test_set_rollout_rejects_invalid(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(PromptRegistryError, match="区间"):
        registry.set_rollout("reflect", "v2", 0.0)
    with pytest.raises(PromptRegistryError, match="active"):
        registry.set_rollout("reflect", "v1", 0.1)
    with pytest.raises(PromptRegistryError, match="版本文件不存在"):
        registry.set_rollout("reflect", "v9", 0.1)


def test_rollback_restores_version_and_clears_rollout(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    registry = PromptRegistry(root)
    registry.set_rollout("reflect", "v2", 0.9)
    registry.rollback("reflect", "v1")

    assert registry.active_version("reflect") == "v1"
    assert registry.get("reflect", rollout_key="any-key").version == "v1"

    reloaded = PromptRegistry(root)
    assert reloaded.active_version("reflect") == "v1"
    assert reloaded.get("reflect", rollout_key="any-key").version == "v1"


def test_rollback_to_v2_and_back(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    registry.rollback("reflect", "v2")
    assert registry.get("reflect").text == "REFLECT-V2"

    registry.rollback("reflect", "v1")
    assert registry.get("reflect").text == "REFLECT-V1"


def test_rollback_missing_version_fails(tmp_path: Path) -> None:
    with pytest.raises(PromptRegistryError, match="版本文件不存在"):
        _registry(tmp_path).rollback("reflect", "v9")


# ── Provider 集成 ──


def _settings() -> Settings:
    return Settings(_env_file=None, llm_api_key=SecretStr(_API_KEY))


def _context() -> LLMContext:
    return LLMContext(messages=[LLMMessage(role="user", content="查订单")])


def _summary() -> ExecutionSummary:
    return ExecutionSummary(tool_executions=[], needs_more_steps=False)


def _memory() -> MemorySnapshot:
    return MemorySnapshot()


def _body(content: str) -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _captured_messages(route: Any) -> list[dict[str, Any]]:
    payload = json.loads(route.calls.last.request.content)
    messages = payload["messages"]
    assert isinstance(messages, list)
    return messages


@respx.mock
async def test_provider_uses_registry_prompt_for_reflect(tmp_path: Path) -> None:
    config = _config()
    config["reflect"]["active"] = "v2"
    registry = _registry(tmp_path, config=config)
    route = respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(json.dumps({"continue_execution": False})))
    )
    async with OpenAICompatibleProvider(
        _settings(), prompts=registry, rollout_key="s1"
    ) as provider:
        await provider.reflect(_context(), _summary(), _memory())

    content = _captured_messages(route)[-1]["content"]
    assert "REFLECT-V2" in content
    assert "REFLECT-V1" not in content


@respx.mock
async def test_provider_uses_registry_prompt_for_summarize(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    route = respx.post(_URL).mock(
        return_value=httpx.Response(200, json=_body(json.dumps({"summary_text": "摘要"})))
    )
    async with OpenAICompatibleProvider(_settings(), prompts=registry) as provider:
        await provider.summarize([LLMMessage(role="user", content="旧对话")])

    messages = _captured_messages(route)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "SUMMARIZE-V1"


@respx.mock
async def test_provider_without_registry_keeps_builtin_prompt() -> None:
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json=_body("ok")))
    async with OpenAICompatibleProvider(_settings()) as provider:
        await provider.generate_response(_context(), _summary(), _memory())

    content = _captured_messages(route)[-1]["content"]
    assert "基于执行摘要生成面向用户的答复" in content
