"""M7-T01：数据分级与脱敏单测。

覆盖字段分级、部分掩码/哈希/拒出、嵌套行、自由文本 PII、
日志 patcher 出口，以及主循环工具结果出口的脱敏。
"""

from typing import Any, cast

from agent.core import AgentCore
from agent.tools import build_default_registry
from config.settings import Settings
from infrastructure.logger import _inject_trace_id
from kingdee.client import KingdeeClient
from models.kingdee import BillQueryParams, BillQueryResult, JsonScalar
from models.llm import (
    PlanResult,
    ReflectionResult,
    ToolCallDecision,
)
from safety.datasecurity import (
    REDACTED,
    classify_field,
    mask_value,
    redact_row,
    redact_rows,
    redact_text,
)
from tests.fakes import FakeProvider


class _FakeClient:
    async def execute_bill_query(self, params: BillQueryParams) -> BillQueryResult:
        row: dict[str, JsonScalar] = {
            "FBillNo": "SO000008",
            "FPhone": "13812345678",
            "FIDCard": "110101199001011234",
            "FContact": "张三",
            "FAmount": 1000,
        }
        return BillQueryResult(
            form_id=params.form_id,
            field_keys=params.field_keys.split(","),
            rows=[row],
        )


# ── 字段分级 ──


def test_classify_exact_leaf_and_default() -> None:
    assert classify_field("FPhone") == "sensitive"
    assert classify_field("FCustomerID.FPhone") == "sensitive"
    assert classify_field("FIDCard") == "high_sensitive"
    assert classify_field("FBillNo") == "internal"
    assert classify_field("FUnknown") == "internal"


# ── 掩码算法 ──


def test_phone_is_partially_masked() -> None:
    assert redact_row({"FPhone": "13812345678"})["FPhone"] == "138****5678"
    assert redact_row({"FCustomerID.FMobile": "13900001111"})["FCustomerID.FMobile"] == (
        "139****1111"
    )


def test_high_sensitive_is_redacted() -> None:
    assert redact_row({"FIDCard": "110101199001011234"})["FIDCard"] == REDACTED
    assert redact_row({"FSalary": 20000})["FSalary"] == REDACTED


def test_generic_sensitive_field_is_masked() -> None:
    assert redact_row({"FContact": "张三"})["FContact"] == "张*"
    assert redact_row({"FAddress": "深圳市南山区科技园"})["FAddress"] == "深*******园"


def test_mask_value_supports_hash_none_and_drop() -> None:
    hashed = mask_value("13812345678", "hash", field_name="FPhone")
    assert isinstance(hashed, str)
    assert len(hashed) == 12
    assert hashed != "13812345678"
    assert mask_value("x", "none") == "x"
    assert mask_value("x", "drop") == REDACTED
    assert mask_value(None, "partial") is None


def test_partial_mask_variants_by_field_name() -> None:
    # 邮箱：合法地址走局部掩码；非法地址退化为通用掩码
    assert mask_value("alice@example.com", "partial", field_name="FEmail") == "a***@example.com"
    assert mask_value("not-an-email", "partial", field_name="FEmail") == "n**********l"
    # 身份证 / 银行卡：直连算法库（配置中高敏默认拒出，这里验证算法本身）
    assert mask_value("110101199001011234", "partial", field_name="FIDCard") == (
        "110101********1234"
    )
    assert mask_value("6222021234567890123", "partial", field_name="FBankAccount") == (
        "**** **** **** 0123"
    )
    # 手机号长度不符：不走手机掩码，退化为通用掩码
    assert mask_value("12345", "partial", field_name="FPhone") == "1***5"


def test_generic_mask_handles_empty_and_short_text() -> None:
    assert redact_row({"FContact": ""})["FContact"] == ""
    assert redact_row({"FContact": "A"})["FContact"] == "*"
    assert redact_row({"FContact": "AB"})["FContact"] == "A*"


def test_internal_field_unchanged() -> None:
    assert redact_row({"FBillNo": "SO000008", "FAmount": 1000}) == {
        "FBillNo": "SO000008",
        "FAmount": 1000,
    }


# ── 嵌套行 ──


def test_nested_row_and_list_are_redacted() -> None:
    row = {
        "FBillNo": "SO1",
        "FEntries": [{"FPhone": "13812345678"}, {"FContact": "李四"}],
        "FCustomerID": {"FIDCard": "110101199001011234", "FName": "客户A"},
    }

    result = redact_row(row)

    assert result["FBillNo"] == "SO1"
    assert result["FEntries"][0]["FPhone"] == "138****5678"
    assert result["FEntries"][1]["FContact"] == "李*"
    assert result["FCustomerID"]["FIDCard"] == REDACTED
    assert result["FCustomerID"]["FName"] == "客户A"


def test_redact_rows_does_not_mutate_input() -> None:
    rows = [{"FPhone": "13812345678"}]

    redacted = redact_rows(rows)

    assert rows[0]["FPhone"] == "13812345678"
    assert redacted[0]["FPhone"] == "138****5678"


# ── 自由文本 ──


def test_redact_text_covers_common_pii() -> None:
    text = (
        "手机 13812345678，身份证 110101199001011234，"
        "银行卡 6222021234567890123，邮箱 alice@example.com"
    )

    masked = redact_text(text)

    assert "138****5678" in masked
    assert "110101********1234" in masked
    assert "**** **** **** 0123" in masked
    assert "a***@example.com" in masked
    assert "13812345678" not in masked
    assert "110101199001011234" not in masked


def test_redact_text_leaves_normal_text() -> None:
    assert redact_text("查询订单 SO000008 的交货日期") == "查询订单 SO000008 的交货日期"


def test_redact_text_edge_cases() -> None:
    assert redact_text("") == ""
    assert redact_text("邮箱 a@example.com") == "邮箱 ***@example.com"


# ── 日志出口 ──


def test_log_patcher_redacts_message() -> None:
    record: dict[str, Any] = {"extra": {}, "message": "客户手机 13812345678"}

    _inject_trace_id(record)  # type: ignore[arg-type]

    assert record["message"] == "客户手机 138****5678"
    assert "trace_id" in record["extra"]


# ── 主循环工具结果出口 ──


async def test_agent_core_redacts_tool_result_rows() -> None:
    provider = FakeProvider(
        plan_queue=[
            PlanResult(
                tool_calls=[
                    ToolCallDecision(
                        tool_name="kingdee_query_sales_order",
                        arguments={"bill_no": "SO000008"},
                    )
                ]
            )
        ],
        reflection_queue=[ReflectionResult(continue_execution=False)],
    )
    client = _FakeClient()
    core = AgentCore(
        provider,
        build_default_registry(),
        cast(KingdeeClient, client),
        Settings(_env_file=None, agent_max_steps=3),
    )

    response = await core.run("查订单 SO000008")

    assert response.trace is not None
    row = response.trace.tool_executions[0].result["rows"][0]
    assert row["FBillNo"] == "SO000008"
    assert row["FPhone"] == "138****5678"
    assert row["FIDCard"] == REDACTED
    assert row["FContact"] == "张*"
    assert row["FAmount"] == 1000
