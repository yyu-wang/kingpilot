"""CLI 本地沙箱演示入口（M3-T07，开发辅助，不进 wheel）。

生产入口是 ``kingpilot-chat``（``agent.cli:main``，凭证读 ``.env``）。
本脚本把金蝶配置指向本地仿真沙箱，用于「连续 5 轮指代理解」人工验收——
LLM 仍走 ``.env`` 的真实路由与密钥（沙箱只替换金蝶侧凭证）。

用法：
    make sandbox          # 终端 1：起仿真沙箱
    make chat             # 终端 2：多轮对话（默认 default 会话，可续聊）
    make chat ARGS="--new"   # 或 --list 查看本地会话
"""

import argparse

from pydantic import SecretStr

from agent.cli import main
from config.settings import Settings, get_settings
from sandbox import DEFAULT_SANDBOX_URL
from sandbox.kingdee_stub.app import (
    SANDBOX_ACCT_ID,
    SANDBOX_APP_ID,
    SANDBOX_APP_SECRET,
    SANDBOX_USER,
)


def _sandbox_settings(server_url: str) -> Settings:
    """在真实 .env 配置上覆盖金蝶侧字段（LLM 密钥/路由保持不变）。"""
    return get_settings().model_copy(
        update={
            # 沙箱是金蝶 K3Cloud 契约仿真，厂商固定为 kingdee
            "erp_vendor": "kingdee",
            "kingdee_server_url": server_url,
            "kingdee_acct_id": SANDBOX_ACCT_ID,
            "kingdee_user": SANDBOX_USER,
            "kingdee_app_id": SANDBOX_APP_ID,
            "kingdee_app_secret": SecretStr(SANDBOX_APP_SECRET),
            "kingdee_timeout_seconds": 5.0,
            # 沙箱凭证必须走 env，避免 file/vault 覆盖成真实环境密钥
            "secret_provider": "env",
        }
    )


def main_with_sandbox() -> int:
    """解析 ``--sandbox-url``，其余参数原样交给 ``agent.cli.main``。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--sandbox-url", default=DEFAULT_SANDBOX_URL)
    sandbox_args, rest = parser.parse_known_args()
    return main(rest, settings=_sandbox_settings(sandbox_args.sandbox_url))


if __name__ == "__main__":
    raise SystemExit(main_with_sandbox())
