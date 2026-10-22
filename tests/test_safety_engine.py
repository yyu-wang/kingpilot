"""M4-T10：安全层编排单测（SafetyLayer）。

覆盖工具过滤、Guard 拦截、权限/行/列、风险取最高、HITL 四档与换码参数。
"""

from datetime import datetime
from typing import Any, cast

from pydantic import Field

from agent.tools import (
    EntityReference,
    ToolDefinition,
    ToolRegistry,
    ToolType,
    build_default_registry,
)
from erp import BillQuerySpec, FilterExpression, eq
from kingdee.client import KingdeeClient
from kingdee.endpoints.base import EndpointDescriptor, EndpointParams, FieldDescriptor
from models.base import KingPilotBaseModel, Role
from models.kingdee import BillQueryResult, JsonScalar
from models.llm import ToolCallDecision, ToolSpec
from models.user import UserContext
from safety.engine import SafetyLayer


class FakeIO:
    """按顺序返回预设答案的确认接口。"""

    def __init__(self, answers: list[bool] | None = None) -> None:
        self._answers = list(answers or [])
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self._answers.pop(0) if self._answers else False


class _FakeClient:
    """固定返回一行客户数据的假客户端（实体预检用，T05 起收中性 BillQuerySpec）。"""

    def __init__(self) -> None:
        self.queries: list[BillQuerySpec] = []

    async def query(self, spec: BillQuerySpec) -> BillQueryResult:
        self.queries.append(spec)
        row: dict[str, JsonScalar] = {
            "FID": "100001",
            "FNumber": "CUS001",
            "FName": "客户A",
            "FBillNo": "SO1",
        }
        return BillQueryResult(
            form_id=spec.form_id,
            field_keys=list(spec.field_keys),
            rows=[row],
        )


class _EmptyClient:
    """实体预检永远查不到。"""

    async def query(self, spec: BillQuerySpec) -> BillQueryResult:
        return BillQueryResult(
            form_id=spec.form_id,
            field_keys=list(spec.field_keys),
            rows=[],
        )


class _WriteParams(EndpointParams):
    customer_number: str = Field(default="", description="客户内码")
    amount: float = Field(default=0.0, description="金额")


class _QueryParams(EndpointParams):
    bill_no: str = Field(default="", description="单据编号")


def _empty_filter(params: KingPilotBaseModel) -> FilterExpression:
    return FilterExpression()


def _bill_filter(params: KingPilotBaseModel) -> FilterExpression:
    p = cast(_QueryParams, params)
    if not p.bill_no:
        return FilterExpression()
    return FilterExpression(conditions=(eq("FBillNo", p.bill_no),))


def _endpoint(form_id: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        form_id=form_id,
        label=form_id,
        fields=(
            FieldDescriptor(
                name="FBillNo", data_type="string", filterable=True, description="编号"
            ),
        ),
        default_field_keys=("FBillNo",),
        default_sort="FBillNo",
    )


def _tool(
    name: str,
    form_id: str,
    *,
    tool_type: ToolType = "query",
    action: str = "",
    params_model: type[EndpointParams] = _QueryParams,
    build_filter: Any = _bill_filter,
    references: tuple[EntityReference, ...] = (),
    required: tuple[str, ...] = (),
) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name=name,
            description="测试工具",
            parameters=params_model.model_json_schema(),
        ),
        params_model=params_model,
        build_filter=build_filter,
        endpoint=_endpoint(form_id),
        tool_type=tool_type,
        action=action,
        entity_references=references,
        required_parameters=required,
    )


_CUSTOMER_REF = EntityReference(parameter="customer_number", entity_type="customer")

_WRITE_TOOL = _tool(
    "test_create_sales_order",
    "SAL_Order",
    tool_type="write",
    action="save",
    params_model=_WriteParams,
    build_filter=_empty_filter,
    references=(_CUSTOMER_REF,),
    required=("customer_number",),
)

_FINANCE_QUERY = _tool("erp_query_finance_vouchers", "GL_Voucher")

_FINANCE_WRITE = _tool(
    "erp_delete_finance_voucher",
    "GL_Voucher",
    tool_type="write",
    action="delete",
    params_model=_WriteParams,
    build_filter=_empty_filter,
    references=(_CUSTOMER_REF,),
    required=("customer_number",),
)


def _registry() -> ToolRegistry:
    registry = build_default_registry()
    for tool in (_WRITE_TOOL, _FINANCE_QUERY, _FINANCE_WRITE):
        registry.register(tool)
    return registry


def _user(role: Role = "operator", **scope: Any) -> UserContext:
    return UserContext(user_id="u001", role=role, data_scope=dict(scope))


def _call(tool_name: str, arguments: dict[str, Any]) -> ToolCallDecision:
    return ToolCallDecision(tool_name=tool_name, arguments=arguments)


def _layer(io: FakeIO | None = None) -> tuple[SafetyLayer, _FakeClient]:
    client = _FakeClient()
    layer = SafetyLayer(
        _registry(),
        cast(KingdeeClient, client),
        io or FakeIO(),
        clock=lambda: datetime(2026, 10, 19, 10, 0),
    )
    return layer, client


# ── 工具过滤 ──


def test_visible_tools_filters_finance_for_operator() -> None:
    layer, _ = _layer()

    names = [spec.name for spec in layer.visible_tools(_user("operator"))]

    assert "erp_query_finance_vouchers" not in names
    assert "erp_query_sales_order" in names


# ── 拦截与放行 ──


async def test_review_blocks_finance_for_operator() -> None:
    layer, _ = _layer()

    verdict = await layer.review(
        _user("operator"),
        [_call("erp_query_finance_vouchers", {"bill_no": "V1"})],
        "trace-1",
    )

    assert verdict.proceed is False
    assert verdict.blocked is True
    assert "财务数据的访问权限" in verdict.message
    assert verdict.guard_records


async def test_review_allows_sales_query() -> None:
    layer, _ = _layer()

    verdict = await layer.review(
        _user("operator"),
        [_call("erp_query_sales_order", {"bill_no": "SO1"})],
        "trace-1",
    )

    assert verdict.proceed is True
    assert verdict.blocked is False
    assert verdict.calls[0].tool_name == "erp_query_sales_order"
    assert verdict.calls[0].arguments == {"bill_no": "SO1"}
    assert verdict.risk is not None and verdict.risk.level == "auto"
    assert verdict.permission is not None
    assert verdict.hitl is not None and verdict.hitl.approved is True
    assert len(verdict.guard_records) == 7


async def test_review_empty_calls_proceeds_without_hitl() -> None:
    layer, _ = _layer()

    verdict = await layer.review(_user("operator"), [], "trace-1")

    assert verdict.proceed is True
    assert verdict.calls == ()
    assert verdict.risk is None
    assert verdict.hitl is None


async def test_guard_records_cover_all_guards() -> None:
    layer, _ = _layer()

    verdict = await layer.review(
        _user("operator"),
        [_call("erp_query_sales_order", {"bill_no": "SO1"})],
        "trace-1",
    )

    names = [record["guard"] for record in verdict.guard_records]
    assert names == [
        "guard_permission",
        "guard_schema",
        "guard_filter",
        "guard_business",
        "guard_entity",
        "guard_limit",
        "guard_params",
    ]


# ── 换码、行/列、HITL ──


async def test_review_uses_entity_fid_after_conversion() -> None:
    layer, _ = _layer(FakeIO([True, True]))

    verdict = await layer.review(
        _user("salesperson"),
        [_call("test_create_sales_order", {"customer_number": "客户A", "amount": 1000})],
        "trace-1",
    )

    assert verdict.proceed is True
    assert verdict.calls[0].arguments["customer_number"] == "100001"


async def test_review_applies_row_filter() -> None:
    layer, _ = _layer()

    verdict = await layer.review(
        _user("operator", org_ids=["ORG001"]),
        [_call("erp_query_sales_order", {"bill_no": "SO1"})],
        "trace-1",
    )

    assert verdict.calls[0].row_filter == FilterExpression(
        conditions=(eq("FSaleOrgId.FNumber", "ORG001"),)
    )


async def test_review_applies_column_deny() -> None:
    layer, _ = _layer()

    verdict = await layer.review(
        _user("finance_staff"),
        [_call("erp_query_finance_vouchers", {"bill_no": "V1"})],
        "trace-1",
    )

    assert verdict.calls[0].denied_fields == ("FBankAccount",)


async def test_review_write_confirm_cancelled() -> None:
    io = FakeIO([False])
    layer, _ = _layer(io)

    verdict = await layer.review(
        _user("salesperson"),
        [
            _call(
                "test_create_sales_order",
                {"customer_number": "100001", "amount": 50_000},
            )
        ],
        "trace-1",
    )

    assert verdict.proceed is False
    assert verdict.cancelled is True
    assert verdict.calls == ()
    assert len(io.prompts) == 1


async def test_review_write_confirm_accepted() -> None:
    io = FakeIO([True])
    layer, _ = _layer(io)

    verdict = await layer.review(
        _user("salesperson"),
        [
            _call(
                "test_create_sales_order",
                {"customer_number": "100001", "amount": 50_000},
            )
        ],
        "trace-1",
    )

    assert verdict.proceed is True
    assert verdict.calls
    assert verdict.hitl is not None and verdict.hitl.level == "confirm"


async def test_review_permission_escalation_requires_dual_approval() -> None:
    io = FakeIO([True, True])
    layer, _ = _layer(io)

    verdict = await layer.review(
        _user("salesperson"),
        [
            _call(
                "test_create_sales_order",
                {"customer_number": "100001", "amount": 150_000},
            )
        ],
        "trace-1",
    )

    assert verdict.proceed is True
    assert verdict.hitl is not None and verdict.hitl.level == "dual_approve"
    assert len(io.prompts) == 2


async def test_review_missing_entity_returns_user_message() -> None:
    client = _EmptyClient()
    layer = SafetyLayer(
        _registry(),
        cast(KingdeeClient, client),
        FakeIO(),
        clock=lambda: datetime(2026, 10, 19, 10, 0),
    )

    verdict = await layer.review(
        _user("salesperson"),
        [_call("test_create_sales_order", {"customer_number": "编造客户", "amount": 1})],
        "trace-1",
    )

    assert verdict.blocked is True
    assert "未找到客户「编造客户」" in verdict.message


async def test_review_read_only_does_not_execute() -> None:
    layer, _ = _layer()

    verdict = await layer.review(
        _user("finance_staff"),
        [
            _call(
                "erp_delete_finance_voucher",
                {"customer_number": "100001", "amount": 1_000_000},
            )
        ],
        "trace-1",
    )

    assert verdict.proceed is False
    assert verdict.read_only is True
    assert verdict.message
    assert "人工执行" in verdict.message
