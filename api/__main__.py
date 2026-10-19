"""``python -m api`` / ``kingpilot-api`` 启动入口（M9-T01）。

生产部署假设单 worker（进程内会话锁只保护单进程）；
多实例/多 worker 的跨进程隔离归 M9-T11。
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="kingpilot-api", description="KingPilot HTTP API")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    args = parser.parse_args()
    uvicorn.run("api.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
