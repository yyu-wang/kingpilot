"""M9-T01 API 测试设施。

测试策略：
- LLM 一律 ``FakeProvider``（零网络）
- 金蝶走 conftest 的真 uvicorn 仿真沙箱（验证 URL/认证/协议）
- Settings 的会话/Trace/审计目录指向 tmp_path，避免污染本机
"""

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from config.settings import Settings
from sandbox import sandbox_settings


def api_settings(
    base_url: str,
    tmp_path: Path,
    keys: Mapping[str, Mapping[str, Any]],
) -> Settings:
    """构造指向仿真沙箱的 Settings，并把明文测试 key 转成 SHA-256 哈希配置。"""
    hashed = {
        hashlib.sha256(raw_key.encode("utf-8")).hexdigest(): dict(identity)
        for raw_key, identity in keys.items()
    }
    return sandbox_settings(base_url).model_copy(
        update={
            "api_key_hashes": SecretStr(json.dumps(hashed)),
            "session_store_dir": tmp_path / "sessions",
            "trace_store_dir": tmp_path / "traces",
            "audit_log_path": tmp_path / "audit.jsonl",
            "approval_store_dir": tmp_path / "approvals",
            "ledger_db_path": tmp_path / "ledger.sqlite3",
            "online_sample_rate": 0.0,
        }
    )


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """跑真 lifespan + ASGI 传输，等价于进程内的真实请求路径。"""
    async with app.router.lifespan_context(app):
        # raise_app_exceptions=False：让 500 处理器把未预期异常转成统一信封，
        # 而不是在测试传输层直接抛出（与生产 uvicorn 的响应行为一致）
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
