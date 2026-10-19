"""M1-T06b：KingdeeClient → 仿真沙箱 真 HTTP 集成验收（标记 integration，默认不跑）。

与 T04 respx 单测的分工：respx 验「client 逻辑分支」（打桩按 URL 匹配即够）；
本文件验「全链路契约」——真 uvicorn + 真 TCP 回环 + 真 cookie jar，
URL 拼接、报文序列化、Cookie 携带任何一环出错都会红。这正是 T06a 卡片
「进程内 mock 无法暴露传输层缺陷」的兑现处。

运行方式：``make test-integration``（即 ``pytest -m integration``）。
真凭证补验（T01 到位后）：把 conftest 的 ``client`` fixture 里的
``sandbox_settings(...)`` 换成 ``Settings()``（读 .env 真凭证）即可，用例零改动。
M2-T04：沙箱 fixture 上移 conftest（本文件与 test_integration_query.py 共用）。
"""

import pytest

from agent.tools import build_default_registry
from infrastructure.exceptions import KingdeeAPIError
from kingdee.client import KingdeeClient
from models.kingdee import BillQueryParams, BillQueryResult
from sandbox import sandbox_settings
from sandbox.kingdee_stub import reset_sessions
from scripts.query_sal_orders import run_query

# 本文件全部用例均为 integration：默认门禁（-m "not integration"）排除，显式指定才跑
pytestmark = pytest.mark.integration


async def test_query_round_trip_over_real_http(client: KingdeeClient) -> None:
    """登录 → 查询全链路；锚点值取自 dataset 具名样例 SO000001。"""
    params = BillQueryParams(
        form_id="SAL_Order",
        field_keys="FBillNo,FDocumentStatus,FBillAllAmount",
        limit=3,
    )

    result = await client.execute_bill_query(params)

    assert result.form_id == "SAL_Order"
    assert result.row_count == 3
    assert result.rows[0]["FBillNo"] == "SO000001"
    assert result.rows[0]["FDocumentStatus"] == "C"
    # 金额保持 10 位小数字符串原样（T02 决策：协议层不做精度转换）
    assert result.rows[0]["FBillAllAmount"] == "150000.0000000000"


async def test_query_pagination_over_real_http(client: KingdeeClient) -> None:
    """130 条种子数据天然跨页：第一页 100 条，第二页 30 条且单据编号衔接。"""
    first = await client.execute_bill_query(
        BillQueryParams(form_id="SAL_Order", field_keys="FBillNo", limit=100)
    )
    second = await client.execute_bill_query(
        BillQueryParams(form_id="SAL_Order", field_keys="FBillNo", start_row=100, limit=100)
    )

    assert first.row_count == 100
    assert second.row_count == 30
    assert first.rows[-1]["FBillNo"] == "SO000100"
    assert second.rows[0]["FBillNo"] == "SO000101"


async def test_session_loss_triggers_relogin_replay(client: KingdeeClient) -> None:
    """服务端踢会话 → client 收到「会话信息丢失」→ 自动重登并重放成功（T04 三层之三）。

    respx 单测只能模拟响应序列；这里是真的「服务端会话表被清空、cookie 过期」，
    验证重登重放机制在真实传输层下成立。
    """
    params = BillQueryParams(form_id="SAL_Order", field_keys="FBillNo", limit=1)

    first = await client.execute_bill_query(params)  # 正常建立会话
    assert first.row_count == 1

    reset_sessions()  # 服务端抹掉全部会话（模拟真金蝶踢下线）；client 仍持旧 cookie

    replayed = await client.execute_bill_query(params)  # 必须自动重登后重放成功
    assert replayed.row_count == 1


async def test_business_error_no_retry_over_real_http(sandbox_server: str) -> None:
    """未知字段 = 确定性业务失败：立即抛 KingdeeAPIError，一次退避都不发生。

    注入记录型 sleep：若业务错误被误入重试路径，sleep 调用记录会直接暴露。
    """
    sleep_calls: list[float] = []

    async def tracking_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async with KingdeeClient(
        sandbox_settings(sandbox_server), sleep=tracking_sleep
    ) as tracking_client:
        params = BillQueryParams(form_id="SAL_Order", field_keys="FBillNo,FBogus")
        with pytest.raises(KingdeeAPIError, match="业务错误"):
            await tracking_client.execute_bill_query(params)

    assert sleep_calls == []


async def test_script_run_query_smoke(
    sandbox_server: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """查询脚本的 ``run_query`` 对真沙箱跑通并打印表格（import 调用，不 subprocess）。"""
    await run_query(sandbox_server)

    captured = capsys.readouterr()
    assert "SO000001" in captured.out
    assert "150000.0000000000" in captured.out
    assert "共 5 行" in captured.out


async def test_tool_execute_sales_order_end_to_end(client: KingdeeClient) -> None:
    """M2-T03：销售订单工具端到端——结构化参数 → FilterString 组装 → 沙箱 → dict 行。

    验证工具层不是纸面设计：LLM 侧 JSON 参数真的能一路走到仿真金蝶并拿回锚点数据。
    """
    tool = build_default_registry().get("kingdee_query_sales_order")

    result = await tool.execute(client, {"limit": 5, "document_status": "C"})

    assert isinstance(result, BillQueryResult)
    assert result.form_id == "SAL_Order"
    assert result.row_count == 5
    assert any(row["FBillNo"] == "SO000001" for row in result.rows)
