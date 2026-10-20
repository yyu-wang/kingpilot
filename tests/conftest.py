"""pytest 公共 fixture（M0-T04 起）。

关键：每个用例前清空 KINGPILOT_* 环境变量并复位配置单例，
避免本机 .env / 外部环境变量污染断言。
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator

import pytest
import uvicorn

from config.settings import get_settings
from kingdee.client import KingdeeClient
from sandbox import sandbox_settings
from sandbox.kingdee_stub import app as sandbox_app
from sandbox.kingdee_stub import reset_dataset, reset_sessions

_ENV_PREFIX = "KINGPILOT_"

# uvicorn 启动就绪的等待上限（本机毫秒级，5s 已是宽裕保护）
_STARTUP_TIMEOUT = 5.0


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


@pytest.fixture
async def sandbox_server() -> AsyncIterator[str]:
    """同进程托管真 uvicorn 仿真沙箱（M1-T06b 建，M2-T04 上移 conftest 跨文件复用）。

    Why 不用 subprocess：子进程要轮询端口、清理脆弱，且无法直接调
    ``reset_sessions()`` 构造「踢会话」场景；同进程 uvicorn 本身就是真 HTTP 服务，
    传输层（TCP/cookie/报文）与独立进程没有差别。fixture 惰性：默认门禁
    （不请求本 fixture 的用例）不会为它起服务。
    """
    reset_sessions()
    reset_dataset()
    config = uvicorn.Config(sandbox_app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not server.started:
        if task.done() or time.monotonic() > deadline:
            raise RuntimeError("仿真沙箱启动失败（超时或异常退出）")
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task
    reset_sessions()
    reset_dataset()


@pytest.fixture
async def client(sandbox_server: str) -> AsyncIterator[KingdeeClient]:
    """指向仿真沙箱的 KingdeeClient（真凭证补验时替换 settings 构造即可）。"""
    async with KingdeeClient(sandbox_settings(sandbox_server)) as kingdee_client:
        yield kingdee_client
