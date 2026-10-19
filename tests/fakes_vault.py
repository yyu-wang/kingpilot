"""本地 Vault KV v2 假服务（M9-T10 契约测试用）。

Why 真 HTTP 服务而非 respx：本项目已固化教训——契约替身必须走真实
TCP/请求头/状态码，才能验证 URL 拼接、Token 头与超时路径。
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, Response
from fastapi.responses import JSONResponse


@dataclass
class VaultState:
    """假 Vault 的可变状态（测试直接改）。"""

    token: str = "test-token"
    data: dict[str, str] = field(
        default_factory=lambda: {
            "llm_api_key": "test-key-a",
            "kingdee_app_secret": "test-secret-a",
        }
    )
    status: int = 200
    payload: Any | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def create_vault_app(state: VaultState) -> FastAPI:
    """KV v2 读取端点；支持用 state 模拟 403/404/500/坏结构。"""
    app = FastAPI()

    @app.get("/v1/{mount}/data/{path:path}")
    def read_secret(
        mount: str,
        path: str,
        x_vault_token: str | None = Header(default=None, alias="X-Vault-Token"),
    ) -> Response:
        with state.lock:
            if state.status != 200:
                return JSONResponse(status_code=state.status, content={"errors": ["fake"]})
            if x_vault_token != state.token:
                return JSONResponse(status_code=403, content={"errors": ["forbidden"]})
            if state.payload is not None:
                return JSONResponse(status_code=200, content=state.payload)
            return JSONResponse(
                status_code=200,
                content={"data": {"data": dict(state.data), "metadata": {"path": path}}},
            )

    return app


@dataclass
class VaultServer:
    """线程内运行的 uvicorn 实例。"""

    url: str
    state: VaultState
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def start_vault(*, timeout_seconds: float = 5.0) -> VaultServer:
    """启动假 Vault 并等待端口就绪。"""
    state = VaultState()
    config = uvicorn.Config(create_vault_app(state), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_seconds
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("假 Vault 启动失败")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return VaultServer(url=f"http://127.0.0.1:{port}", state=state, server=server, thread=thread)
