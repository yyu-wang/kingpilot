"""仿真沙箱启动入口。

用法：``uv run python -m sandbox.kingdee_stub --port 8000``

Why 默认绑 ``127.0.0.1``：仿真端持有明文占位凭证，只能本机访问，
避免把仿真服务暴露到局域网后被误当成真实金蝶端点使用。
"""

import argparse

import uvicorn

from sandbox.kingdee_stub import app

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def main() -> None:
    """解析命令行参数并启动 uvicorn。"""
    parser = argparse.ArgumentParser(description="启动 KingPilot K3Cloud 契约仿真沙箱")
    parser.add_argument("--host", default=_DEFAULT_HOST, help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT, help="监听端口（默认 8000）")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
