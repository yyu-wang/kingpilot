"""pytest 公共 fixture（M0-T04 起）。

关键：每个用例前清空 KINGPILOT_* 环境变量并复位配置单例，
避免本机 .env / 外部环境变量污染断言。
"""

import os
from collections.abc import Iterator

import pytest

from config.settings import get_settings

_ENV_PREFIX = "KINGPILOT_"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """隔离配置环境：清变量 + 复位 lru_cache 单例。

    使用 monkeypatch 而非直接 delenv：用例结束后自动还原，互不污染。
    """
    for key in list(os.environ):
        if key.startswith(_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
