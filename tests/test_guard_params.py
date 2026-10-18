"""M4-T06：Guard 5（业务必填项 + 引用内码 Id）单测。

覆盖验收点：名称引用（非 Id）拦截、必填缺失拦截；
外加查询跳过、规则缺失、可选引用、管道换码链路闭合。
"""

from typing import Any

from pydantic import Field

from agent.tools import EntityReference, ToolDefinition, ToolRegistry, build_default_registry
from kingdee.endpoints.base import EndpointParams
from kingdee.endpoints.sal_order import SAL_ORDER
from kingdee.entity_lookup import EntityLookupResult
from models.base import KingPilotBaseModel
from models.llm import ToolSpec
from models.user import UserContext
from safety.guard import GuardContext, GuardPipeline
from safety.guard_entity import EntityPreflightGuard
from safety.guard_params import ParamGuard


class _WriteParams(EndpointParams):
    """测试用写工具参数。"""

    customer_number: str = Field(default="", description="客户内码")
    note: str = Field(default="", description="备注")


def _empty_filter(params: KingPilotBaseModel) -> str:
    return ""


def _write_tool(
    name: str,
    *,
    required: tuple[str, ...],
    references: tuple[EntityReference, ...] = (),
) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name=name,
            description="测试用写工具",
            parameters=_WriteParams.model_json_schema(),
        ),
        params_model=_WriteParams,
        build_filter=_empty_filter,
        endpoint=SAL_ORDER,
        tool_type="write",
        entity_references=references,
        required_parameters=required,
    )


_CUSTOMER_REF = EntityReference(parameter="customer_number", entity_type="customer")

_WRITE_TOOL = _write_tool(
    "kingdee_create_sales_order",
    required=("customer_number", "note"),
    references=(_CUSTOMER_REF,),
)

_NO_RULES_TOOL = _write_tool("kingdee_create_without_rules", required=())

_OPTIONAL_REF_TOOL = _write_tool(
    "kingdee_create_optional_customer",
    required=("note",),
    references=(_CUSTOMER_REF,),
)


class FakeLookup:
    """可控实体解析器（仅管道联调用）。"""

    def __init__(self, results: dict[tuple[str, str], EntityLookupResult]) -> None:
        self._results = results

    async def lookup(
        self, entity_type: str, identifier: str, *, allow_name: bool = True
    ) -> EntityLookupResult:
        return self._results.get((entity_type, identifier), EntityLookupResult(found=False))


def _registry() -> ToolRegistry:
    registry = build_default_registry()
    registry.register(_WRITE_TOOL)
    registry.register(_NO_RULES_TOOL)
    registry.register(_OPTIONAL_REF_TOOL)
    return registry


def _context(tool_name: str, arguments: dict[str, Any]) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        arguments=arguments,
        user_context=UserContext(user_id="u001", role="viewer"),
        trace_id="trace-params",
    )


# ── 跳过与规则缺失 ──


async def test_query_tool_is_skipped() -> None:
    result = await ParamGuard(_registry()).check(_context("kingdee_query_sales_order", {}))

    assert result.passed is True
    assert result.detail["skipped"] is True


async def test_write_tool_without_required_rules_blocked() -> None:
    result = await ParamGuard(_registry()).check(
        _context("kingdee_create_without_rules", {"customer_number": "100001"})
    )

    assert result.passed is False
    assert result.detail["branch"] == "missing_rules"


async def test_unknown_tool_blocked() -> None:
    result = await ParamGuard(_registry()).check(_context("kingdee_not_exists", {}))

    assert result.passed is False
    assert "未注册" in result.reason


# ── 业务必填项 ──


async def test_missing_multiple_required_fields_blocked_at_once() -> None:
    result = await ParamGuard(_registry()).check(
        _context("kingdee_create_sales_order", {"customer_number": "", "note": "   "})
    )

    assert result.passed is False
    assert result.detail["missing"] == ["customer_number", "note"]
    assert "customer_number" in result.reason
    assert "note" in result.reason


async def test_required_fields_present_pass() -> None:
    result = await ParamGuard(_registry()).check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "100001", "note": "加急"},
        )
    )

    assert result.passed is True
    assert result.detail["required_checked"] == ["customer_number", "note"]
    assert result.detail["id_checked"] == ["customer_number"]


async def test_missing_required_none_blocked() -> None:
    """参数完全未提供（None）也属于缺失。"""
    result = await ParamGuard(_registry()).check(_context("kingdee_create_sales_order", {}))

    assert result.passed is False
    assert result.detail["missing"] == ["customer_number", "note"]


async def test_empty_container_required_blocked() -> None:
    result = await ParamGuard(_registry()).check(
        _context("kingdee_create_sales_order", {"customer_number": {}, "note": []})
    )

    assert result.passed is False
    assert result.detail["missing"] == ["customer_number", "note"]


# ── 引用字段必须是内码 Id ──


async def test_name_reference_blocked() -> None:
    result = await ParamGuard(_registry()).check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "深圳XX公司", "note": "加急"},
        )
    )

    assert result.passed is False
    assert "内码 Id" in result.reason
    assert result.detail["invalid_ids"][0]["parameter"] == "customer_number"


async def test_numeric_int_id_passes() -> None:
    result = await ParamGuard(_registry()).check(
        _context("kingdee_create_sales_order", {"customer_number": 100001, "note": "加急"})
    )

    assert result.passed is True
    assert result.detail["id_checked"] == ["customer_number"]


async def test_zero_id_blocked() -> None:
    result = await ParamGuard(_registry()).check(
        _context("kingdee_create_sales_order", {"customer_number": "0", "note": "加急"})
    )

    assert result.passed is False
    assert "内码 Id" in result.reason


async def test_bool_reference_blocked() -> None:
    """bool 是 int 的子类，但绝不能当作内码。"""
    result = await ParamGuard(_registry()).check(
        _context("kingdee_create_sales_order", {"customer_number": True, "note": "加急"})
    )

    assert result.passed is False
    assert "内码 Id" in result.reason


async def test_non_scalar_reference_blocked() -> None:
    result = await ParamGuard(_registry()).check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": {"id": 100001}, "note": "加急"},
        )
    )

    assert result.passed is False
    assert "内码 Id" in result.reason


async def test_optional_reference_blank_is_skipped() -> None:
    result = await ParamGuard(_registry()).check(
        _context("kingdee_create_optional_customer", {"customer_number": "", "note": "加急"})
    )

    assert result.passed is True
    assert result.detail["id_checked"] == []


# ── 管道换码链路 ──


async def test_pipeline_applies_entity_updates_before_param_guard() -> None:
    lookup = FakeLookup(
        {
            ("customer", "深圳XX公司"): EntityLookupResult(
                found=True, canonical_id="100001", display_name="深圳XX公司"
            )
        }
    )
    pipeline = GuardPipeline([EntityPreflightGuard(_registry(), lookup), ParamGuard(_registry())])

    result = await pipeline.check(
        _context(
            "kingdee_create_sales_order",
            {"customer_number": "深圳XX公司", "note": "加急"},
        )
    )

    assert result.blocked is False
    assert result.effective_arguments == {
        "customer_number": "100001",
        "note": "加急",
    }
    assert result.records[1].detail["id_checked"] == ["customer_number"]


async def test_pipeline_without_updates_keeps_arguments() -> None:
    pipeline = GuardPipeline([ParamGuard(_registry())])
    arguments = {"customer_number": "100001", "note": "加急"}

    result = await pipeline.check(_context("kingdee_create_sales_order", arguments))

    assert result.blocked is False
    assert result.effective_arguments == arguments
