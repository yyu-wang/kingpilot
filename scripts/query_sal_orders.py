"""硬编码查询脚本（M1-T06b）：验证 KingdeeClient → K3Cloud 全链路。

不走 LLM、不涉及 Agent——就是「一段确定性的调用代码」：组参数（M1-T05 描述符）
→ 经 KingdeeClient（M1-T04）真实发 HTTP → 打印类型化结果。它是 M2 之前
「协议链路已调通」的最直观证据，也是真凭证到位后（M1-T01 补验）的最小复验入口。

用法（本地仿真沙箱）：
    make sandbox        # 终端 1：起仿真沙箱（127.0.0.1:8000）
    make query-demo     # 终端 2：跑本脚本，查 5 笔已审核销售订单

真凭证补验（T01 到位后）：.env 填 5 行真凭证，把 ``_build_settings`` 里的
``sandbox_settings(...)`` 换成 ``get_settings()`` 即可，脚本其余零改动——
这正是「契约替身」换真环境的切换成本演示。
"""

import argparse
import asyncio
import sys

from config.settings import Settings
from erp import FilterExpression, eq
from infrastructure.exceptions import KingPilotError
from kingdee.client import KingdeeClient
from kingdee.endpoints import SAL_ORDER
from models.kingdee import BillQueryResult
from sandbox import sandbox_settings

_DEFAULT_URL = "http://127.0.0.1:8000"
_QUERY_LIMIT = 5  # 演示脚本取小结果集，便于终端阅读


def _build_settings(server_url: str) -> Settings:
    """构造指向目标的配置。

    现阶段指向本地仿真沙箱（凭证是占位值）；真凭证补验时整行换成
    ``return get_settings()``（凭证从 .env 读），脚本其余零改动。
    """
    return sandbox_settings(server_url)


def _print_table(result: BillQueryResult) -> None:
    """把查询结果打印成文本表格（金额保留 10 位小数字符串原样，不做精度转换）。"""
    if not result.rows:
        print("（无数据）")
        return
    headers = result.field_keys
    widths = [
        max(len(key), *(len(str(row.get(key, ""))) for row in result.rows)) for key in headers
    ]
    print(" | ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    print("-+-".join("-" * width for width in widths))
    for row in result.rows:
        print(
            " | ".join(
                str(row.get(key, "")).ljust(width)
                for key, width in zip(headers, widths, strict=True)
            )
        )
    print(f"\n共 {result.row_count} 行（FormId={result.form_id}）")


async def run_query(server_url: str) -> None:
    """执行一次完整查询并打印（拆成纯函数：集成测试直接 import 调用，不 subprocess 跑脚本）。"""
    spec = SAL_ORDER.to_query_params(
        # 已审核单（Guard 2 白名单校验属 M4，此处硬编码安全值）
        filter=FilterExpression(conditions=(eq("FDocumentStatus", "C"),)),
        limit=_QUERY_LIMIT,
    )
    async with KingdeeClient(_build_settings(server_url)) as client:
        result = await client.query(spec)
    _print_table(result)


def main() -> None:
    """命令行入口：解析 --url 后执行查询；金蝶异常统一出口（演示调用方该怎么接）。"""
    parser = argparse.ArgumentParser(description="KingPilot 销售订单查询演示（经 KingdeeClient）")
    parser.add_argument(
        "--url", default=_DEFAULT_URL, help=f"K3Cloud 服务地址（默认 {_DEFAULT_URL}）"
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_query(args.url))
    except KingPilotError as exc:
        # 异常归一化的收益在这行体现：调用方只需接住基类，context 里带 form_id/操作名可定位
        print(f"查询失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
