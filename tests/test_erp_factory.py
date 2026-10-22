"""ERP 客户端工厂单测（M10-T08）。

覆盖：默认/显式厂商、大小写与空白归一、未知厂商 fail-fast、
契约满足（``isinstance ErpClient``）、上下文管理释放、
以及「契约层模块级不依赖厂商包」这一结构约束。
"""

import subprocess
import sys
from typing import Any

import pytest

from config.settings import Settings
from erp import ErpClient
from erp.factory import SUPPORTED_VENDORS, build_erp_client
from infrastructure.exceptions import ConfigurationError
from kingdee.client import KingdeeClient


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_default_vendor_builds_kingdee_client() -> None:
    client = build_erp_client(_settings())

    assert isinstance(client, KingdeeClient)
    assert isinstance(client, ErpClient)


@pytest.mark.parametrize("vendor", ["kingdee", " KingDee ", "KINGDEE"])
def test_vendor_is_normalized(vendor: str) -> None:
    """大小写与首尾空白不影响选择（配置文件常见手写误差）。"""
    client = build_erp_client(_settings(erp_vendor=vendor))

    assert isinstance(client, KingdeeClient)


def test_unknown_vendor_fails_fast() -> None:
    with pytest.raises(ConfigurationError, match="不支持的 ERP 厂商") as exc_info:
        build_erp_client(_settings(erp_vendor="sap"))

    assert exc_info.value.context["erp_vendor"] == "sap"
    assert exc_info.value.context["supported"] == list(SUPPORTED_VENDORS)


def test_empty_vendor_rejected_by_settings() -> None:
    """空厂商在 Settings 层就被拒绝，不进入工厂。"""
    with pytest.raises(ValueError):
        _settings(erp_vendor="")


async def test_context_manager_releases_and_aclose_is_idempotent() -> None:
    async with build_erp_client(_settings()) as client:
        assert isinstance(client, KingdeeClient)
    # 退出上下文已关闭连接池；重复关闭不应抛错
    await client.aclose()


def test_factory_does_not_import_vendor_at_module_level() -> None:
    """契约层模块级不得依赖厂商包，否则 erp ↔ kingdee 形成导入期循环。"""
    code = "import sys, erp.factory; print('kingdee.client' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"
