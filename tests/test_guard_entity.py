"""M4-T04：Guard 3（实体存在性预检）单测。

覆盖验收点：全部存在 / 部分幻觉 / 全部幻觉三分支；
外加查询工具跳过、规则缺失、空值、解析异常、allow_name 透传与管道集成。
"""

from typing import Any

from pydantic import Field

from agent.tools import EntityReference, ToolDefinition, ToolRegistry, build_default_registry
from infrastructure.exceptions import KingdeeAPIError
from kingdee.endpoints.base import EndpointParams
from kingdee.endpoints.sal_order import SAL_ORDER
from kingdee.entity_lookup import EntityLookupResult
from models.base import KingPilotBaseModel
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.guard_entity import EntityPreflightGuard


class _WriteParams(EndpointParams):
    """测试用写工具参数：两个实体引用。"""

    customer_number: str = Field(default="", description="客户编码或名称")
    supplier_number: str = Field(default="", description="供应商编码或名称")


class _StrictParams(EndpointParams):
    """测试用动作工具参数：只允许订单编号。"""

    order_no: str = Field(default="", description="订单编号")


def _empty_filter(params: KingPilotBaseModel) -> str:
    return ""


_WRITE_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_create_sales_order",
        description="测试用：创建销售订单",
        parameters=_WriteParams.model_json_schema(),
    ),
    params_model=_WriteParams,
    build_filter=_empty_filter,
    endpoint=SAL_ORDER,
    tool_type="write",
    entity_references=(
        EntityReference(parameter="customer_number", entity_type="customer"),
        EntityReference(parameter="supplier_number", entity_type="supplier"),
    ),
    required_parameters=("customer_number", "supplier_number"),
)

_NO_RULES_WRITE_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_create_without_rules",
        description="测试用：未声明实体引用的写工具",
        parameters=_WriteParams.model_json_schema(),
    ),
    params_model=_WriteParams,
    build_filter=_empty_filter,
    endpoint=SAL_ORDER,
    tool_type="write",
)

_STRICT_ACTION_TOOL = ToolDefinition(
    spec=ToolSpec(
        name="kingdee_audit_sales_order",
        description="测试用：审核销售订单",
        parameters=_StrictParams.model_json_schema(),
    ),
    params_model=_StrictParams,
    build_filter=_empty_filter,
    endpoint=SAL_ORDER,
    tool_type="action",
    entity_references=(
        EntityReference(parameter="order_no", entity_type="order", allow_name=False),
    ),
    required_parameters=("order_no",),
)


class FakeLookup:
    """可控实体解析器：记录调用参数，可配置结果或异常。"""

    def __init__(
        self,
        results: dict[tuple[str, str], EntityLookupResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._results = results or {}
        self._error = error
        self.calls: list[tuple[str, str, bool]] = []

    async def lookup(
        self, entity_type: str, identifier: str, *, allow_name: bool = True
    ) -> EntityLookupResult:
        self.calls.append((entity_type, identifier, allow_name))
        if self._error is not None:
            raise self._error
        return self._results.get((entity_type, identifier), EntityLookupResult(found=False))


def _registry() -> ToolRegistry:
    registry = build_default_registry()
    registry.register(_WRITE_TOOL)
    registry.register(_NO_RULES_WRITE_TOOL)
    registry.register(_STRICT_ACTION_TOOL)
    return registry


def _context(tool_name: str, arguments: dict[str, Any]) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments=arguments,
        user_context=UserContext(user_id="u001", role="viewer"),
        trace_id="trace-entity",
    )


# ── 跳过与规则缺失 ──


async def test_read_only_tool_is_skipped() -> None:
    lookup = FakeLookup(error=RuntimeError("查询工具不应触发实体解析"))

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context("kingdee_query_sales_order", {})
    )

    assert result.passed is True
    assert result.detail["skipped"] is True
    assert lookup.calls == []


async def test_write_tool_without_entity_rules_blocked() -> None:
    result = await EntityPreflightGuard(_registry(), FakeLookup()).check(
        _context("kingdee_create_without_rules", {"customer_number": "CUS001"})
    )

    assert result.passed is False
    assert result.detail["branch"] == "missing_rules"


async def test_unknown_tool_blocked() -> None:
    result = await EntityPreflightGuard(_registry(), FakeLookup()).check(
        _context("kingdee_not_exists", {})
    )

    assert result.passed is False
    assert "未注册" in result.reason


# ── 三分支 ──


async def test_all_entities_exist_pass_with_updates() -> None:
    lookup = FakeLookup(
        {
            ("customer", "深圳XX公司"): EntityLookupResult(
                found=True, canonical_id="100001", display_name="深圳XX公司"
            ),
            ("supplier", "SUP001"): EntityLookupResult(
                found=True, canonical_id="200001", display_name="深圳供应商"
            ),
        }
    )

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "深圳XX公司", "supplier_number": "SUP001"},
        )
    )

    assert result.passed is True
    assert result.detail["branch"] == "all_valid"
    assert result.detail["parameter_updates"] == {
        "customer_number": "100001",
        "supplier_number": "200001",
    }
    assert len(lookup.calls) == 2


async def test_partial_hallucination_blocked_with_correction_prompt() -> None:
    lookup = FakeLookup(
        {
            ("customer", "深圳XX公司"): EntityLookupResult(
                found=True, canonical_id="100001", display_name="深圳XX公司"
            ),
            # supplier 未配置 → 未命中
        }
    )

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "深圳XX公司", "supplier_number": "不存在的供应商"},
        )
    )

    assert result.passed is False
    assert result.detail["branch"] == "partial"
    assert result.detail["resolved"] == {"customer_number": "100001"}
    assert result.detail["hallucinated"][0]["parameter"] == "supplier_number"
    prompt = result.detail["correction_prompt"]
    assert "supplier_number" in prompt
    assert "系统中不存在" in prompt
    assert "已核实" in prompt
    assert "100001" in prompt


async def test_all_hallucination_blocked_with_user_message() -> None:
    lookup = FakeLookup(
        {
            ("customer", "编造客户"): EntityLookupResult(
                found=False, candidates=("深圳XX科技（CUS009）",)
            ),
        }
    )

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "编造客户", "supplier_number": "编造供应商"},
        )
    )

    assert result.passed is False
    assert result.detail["branch"] == "all_missing"
    assert len(result.detail["hallucinated"]) == 2
    message = result.detail["user_message"]
    assert "未找到实体「编造客户」" in message
    assert "深圳XX科技（CUS009）" in message
    assert "请确认名称或提供编码后重试" in message


# ── 边界与异常 ──


async def test_empty_reference_values_are_skipped() -> None:
    """空值不在这里判必填（Guard 5 职责），也不触发实体解析。"""
    lookup = FakeLookup()

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "  ", "supplier_number": ""},
        )
    )

    assert result.passed is True
    assert result.detail["parameter_updates"] == {}
    assert lookup.calls == []


async def test_lookup_error_is_fail_closed() -> None:
    lookup = FakeLookup(error=KingdeeAPIError("查询接口不可用"))

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context("kingdee_create_sales_order", {"customer_number": "CUS001"})
    )

    assert result.passed is False
    assert result.detail["branch"] == "lookup_error"
    assert result.detail["error_type"] == "KingdeeAPIError"


async def test_found_without_canonical_id_is_unresolved() -> None:
    """解析器返回 found=True 但编码为空：无法锚定 = 拒绝。"""
    lookup = FakeLookup({("customer", "CUS001"): EntityLookupResult(found=True, canonical_id="")})

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context("kingdee_create_sales_order", {"customer_number": "CUS001"})
    )

    assert result.passed is False
    assert result.detail["branch"] == "all_missing"


async def test_allow_name_is_passed_to_lookup() -> None:
    lookup = FakeLookup(
        {
            ("order", "SO000008"): EntityLookupResult(
                found=True, canonical_id="300008", display_name="SO000008"
            )
        }
    )

    result = await EntityPreflightGuard(_registry(), lookup).check(
        _context("kingdee_audit_sales_order", {"order_no": "SO000008"})
    )

    assert result.passed is True
    assert lookup.calls == [("order", "SO000008", False)]


# ── 与 T01 管道集成 ──


async def test_guard_works_inside_pipeline() -> None:
    pipeline = GuardPipeline([EntityPreflightGuard(_registry(), FakeLookup())])

    result = await pipeline.check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "编造客户", "supplier_number": "编造供应商"},
        )
    )

    assert result.blocked is True
    assert result.blocked_by == "guard_entity"
    assert result.records[0].detail["branch"] == "all_missing"
