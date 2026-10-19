"""Prompt 版本管理（M8-T04）：版本文件 + 灰度 + 回滚。

配置单一真源：``prompts/rollout.json``
- ``active``：稳定版本
- ``rollout``：候选版本 → 灰度比例（0 < ratio < 1，总和 ≤ 1）

灰度选择按 ``rollout_key`` 哈希分桶（0~9999），同一 key 永远命中同一版本；
空 key 固定 active，避免匿名流量随机跳版本。

回滚 = active 切到目标版本并清空该 Prompt 的灰度配置，原子写回配置文件。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infrastructure.exceptions import PromptRegistryError

_BUCKETS = 10_000
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CONFIG_FILENAME = "rollout.json"


@dataclass(frozen=True)
class PromptVersion:
    """一次 Prompt 解析结果。"""

    prompt_id: str
    version: str
    text: str


@dataclass(frozen=True)
class _PromptConfig:
    active: str
    rollout: dict[str, float]


class PromptRegistry:
    """文件系统 Prompt 注册中心。"""

    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise PromptRegistryError(f"Prompt 目录不存在: {root}")
        self._root = root
        self._config_path = root / _CONFIG_FILENAME
        self._text_cache: dict[tuple[str, str], str] = {}
        self._config = self._load_config()

    @property
    def root(self) -> Path:
        return self._root

    def get(self, prompt_id: str, *, rollout_key: str = "") -> PromptVersion:
        """按灰度配置选择版本并返回文本。"""
        config = self._require_prompt(prompt_id)
        version = self._select_version(prompt_id, config, rollout_key)
        return PromptVersion(
            prompt_id=prompt_id,
            version=version,
            text=self._read_text(prompt_id, version),
        )

    def active_version(self, prompt_id: str) -> str:
        return self._require_prompt(prompt_id).active

    def list_versions(self, prompt_id: str) -> tuple[str, ...]:
        """列出磁盘上该 Prompt 的全部版本（排序）。"""
        self._require_prompt(prompt_id)
        directory = self._root / prompt_id
        if not directory.is_dir():
            return ()
        return tuple(sorted(path.stem for path in directory.glob("*.txt")))

    def set_rollout(self, prompt_id: str, version: str, ratio: float) -> None:
        """设置灰度比例并原子落盘。"""
        if isinstance(ratio, bool) or not 0.0 < ratio < 1.0:
            raise PromptRegistryError(f"灰度比例必须在 (0, 1) 区间: {ratio!r}")
        config = self._require_prompt(prompt_id)
        self._validate_version_name(version)
        if version == config.active:
            raise PromptRegistryError("灰度版本不能等于 active 版本")
        self._read_text(prompt_id, version)  # 文件不存在即报错
        merged = {**config.rollout, version: float(ratio)}
        if sum(merged.values()) > 1.0:
            raise PromptRegistryError("同一 Prompt 的灰度比例之和不能超过 1")
        self._config[prompt_id] = _PromptConfig(active=config.active, rollout=merged)
        self._write_config()

    def rollback(self, prompt_id: str, version: str) -> None:
        """切回指定版本并清空灰度（回到 100% 稳定版）。"""
        self._require_prompt(prompt_id)
        self._validate_version_name(version)
        self._read_text(prompt_id, version)  # 文件不存在即报错
        self._config[prompt_id] = _PromptConfig(active=version, rollout={})
        self._write_config()

    # ── 内部：配置加载与选择 ──

    def _load_config(self) -> dict[str, _PromptConfig]:
        if not self._config_path.exists():
            raise PromptRegistryError(f"Prompt 配置不存在: {self._config_path}")
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PromptRegistryError(f"Prompt 配置读取失败: {self._config_path}") from exc
        except ValueError as exc:
            raise PromptRegistryError(f"Prompt 配置不是合法 JSON: {self._config_path}") from exc
        if not isinstance(payload, dict) or not payload:
            raise PromptRegistryError("Prompt 配置必须是非空对象")

        config: dict[str, _PromptConfig] = {}
        for prompt_id, raw in payload.items():
            self._validate_prompt_id(str(prompt_id))
            if not isinstance(raw, dict):
                raise PromptRegistryError(f"Prompt {prompt_id} 配置必须是对象")
            active = raw.get("active")
            if not isinstance(active, str) or not active:
                raise PromptRegistryError(f"Prompt {prompt_id} 缺少 active 版本")
            self._validate_version_name(active)
            rollout_raw = raw.get("rollout") or {}
            if not isinstance(rollout_raw, dict):
                raise PromptRegistryError(f"Prompt {prompt_id} rollout 必须是对象")
            rollout = self._validate_rollout(str(prompt_id), rollout_raw)
            self._read_text(str(prompt_id), active)
            for version in rollout:
                self._read_text(str(prompt_id), version)
            config[str(prompt_id)] = _PromptConfig(active=active, rollout=rollout)
        return config

    def _validate_rollout(self, prompt_id: str, raw: dict[str, Any]) -> dict[str, float]:
        rollout: dict[str, float] = {}
        for version, ratio in raw.items():
            self._validate_version_name(str(version))
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
                raise PromptRegistryError(f"Prompt {prompt_id} 的灰度比例必须是数值")
            if not 0.0 < float(ratio) < 1.0:
                raise PromptRegistryError(
                    f"Prompt {prompt_id} 的灰度比例必须在 (0, 1) 区间: {ratio!r}"
                )
            rollout[str(version)] = float(ratio)
        if sum(rollout.values()) > 1.0:
            raise PromptRegistryError(f"Prompt {prompt_id} 的灰度比例之和不能超过 1")
        return rollout

    def _select_version(self, prompt_id: str, config: _PromptConfig, rollout_key: str) -> str:
        if not rollout_key or not config.rollout:
            return config.active
        bucket = _bucket(rollout_key)
        cumulative = 0.0
        for version in sorted(config.rollout):
            cumulative += config.rollout[version]
            if bucket < int(cumulative * _BUCKETS):
                return version
        return config.active

    def _require_prompt(self, prompt_id: str) -> _PromptConfig:
        config = self._config.get(prompt_id)
        if config is None:
            raise PromptRegistryError(f"未知 Prompt: {prompt_id}")
        return config

    def _read_text(self, prompt_id: str, version: str) -> str:
        key = (prompt_id, version)
        cached = self._text_cache.get(key)
        if cached is not None:
            return cached
        path = self._root / prompt_id / f"{version}.txt"
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PromptRegistryError(f"Prompt 版本文件不存在: {path}") from exc
        if not text:
            raise PromptRegistryError(f"Prompt 版本文件为空: {path}")
        self._text_cache[key] = text
        return text

    def _write_config(self) -> None:
        payload = {
            prompt_id: {
                "active": config.active,
                "rollout": dict(sorted(config.rollout.items())),
            }
            for prompt_id, config in sorted(self._config.items())
        }
        tmp = self._config_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self._config_path)

    @staticmethod
    def _validate_prompt_id(prompt_id: str) -> None:
        if not prompt_id or not _NAME_RE.fullmatch(prompt_id):
            raise PromptRegistryError(f"非法 Prompt 名称: {prompt_id!r}")

    @staticmethod
    def _validate_version_name(version: str) -> None:
        if not version or not _NAME_RE.fullmatch(version):
            raise PromptRegistryError(f"非法 Prompt 版本: {version!r}")


def _bucket(rollout_key: str) -> int:
    """确定性分桶：0 ~ 9999，同一 key 永远同一桶。"""
    digest = hashlib.sha256(rollout_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % _BUCKETS
