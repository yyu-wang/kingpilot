"""AgentCore 主循环单测（M3-T01 骨架 + M3-T03 记忆接线，FakeProvider/FakeTool 零网络）。

验收「FakeProvider + FakeTool 下主循环单测走通全 Phase」。
FakeTool（``_EchoTool``）覆盖 ``execute`` 返回固定 BillQueryResult——
不走真 ``ToolDefinition.execute``（那会经 build_filter 碰金蝶 client），
骨架测试只需证明「Phase 3 真执行了工具且结果进了 trace」。
M3-T03 追加：记忆写回口径与 Phase 0 长历史压缩组装。
"""

from typing import Any

from agent.core import AgentCore
from agent.memory import MemoryStore
from agent.tools import ToolDefinition, ToolRegistry
from config.settings import Settings
from erp import FilterExpression
from kingdee.endpoints import SAL_ORDER
from kingdee.endpoints.base import EndpointParams
from models.kingdee import ActionResult, BillQueryResult
from models.llm import (
    PlanResult,
    ReflectionResult,
    SummaryDraft,
    TokenUsage,
    ToolCallDecision,
    ToolSpec,
)
from models.memory import EntityRef
from tests.fakes import FakeProvider

_ECHO_TOOL_NAME = "echo_test_tool"
_CUSTOMER_TOOL_NAME = "customer_query_tool"
_SALES_TOOL_NAME = "erp_query_sales_order"


class _CustomerParams(EndpointParams):
    """客户查询假参数。"""


class _CustomerTool(ToolDefinition):
    """FakeTool：返回 BD_Customer 结果（含 CUS001），验证实体抽取入图。"""

    async def execute(
        self, client: Any, arguments: dict[str, Any], *, row_filter: FilterExpression | None = None
    ) -> BillQueryResult:
        return BillQueryResult(
            form_id="BD_Customer",
            field_keys=["FNumber", "FName"],
            rows=[{"FNumber": "CUS001", "FName": "测试客户"}],
        )


def _make_customer_registry() -> ToolRegistry:
    spec = ToolSpec(
        name=_CUSTOMER_TOOL_NAME,
        description="测试客户查询工具：固定返回 CUS001",
        parameters=_CustomerParams.model_json_schema(),
    )
    registry = ToolRegistry()
    registry.register(
        _CustomerTool(
            spec=spec,
            params_model=_CustomerParams,
            build_filter=lambda _p: FilterExpression(),
            endpoint=SAL_ORDER,
        )
    )
    return registry


class _EchoParams(EndpointParams):
    """空参数模型（limit 由 EndpointParams 提供）。"""


def _echo_build_filter(params: Any) -> FilterExpression:
    return FilterExpression()


class _EchoTool(ToolDefinition):
    """FakeTool：覆盖 execute 返回固定结果，不碰金蝶 client。"""

    async def execute(
        self, client: Any, arguments: dict[str, Any], *, row_filter: FilterExpression | None = None
    ) -> BillQueryResult:
        return BillQueryResult(form_id="ECHO", field_keys=["echo"], rows=[{"echo": "ok"}])


def _make_echo_registry() -> ToolRegistry:
    spec = ToolSpec(
        name=_ECHO_TOOL_NAME,
        description="测试回声工具：固定返回一行 {echo: ok}",
        parameters=_EchoParams.model_json_schema(),
    )
    registry = ToolRegistry()
    registry.register(
        _EchoTool(
            spec=spec,
            params_model=_EchoParams,
            build_filter=_echo_build_filter,
            endpoint=SAL_ORDER,
        )
    )
    return registry


class _SalesTool(ToolDefinition):
    """FakeTool：返回固定销售订单行，验证规则引擎补全后的执行链路。"""

    async def execute(
        self, client: Any, arguments: dict[str, Any], *, row_filter: FilterExpression | None = None
    ) -> BillQueryResult:
        return BillQueryResult(
            form_id="SAL_Order",
            field_keys=["FBillNo", "FBillAllAmount"],
            rows=[{"FBillNo": "SO000001", "FBillAllAmount": "150000.0000000000"}],
        )


def _make_sales_registry() -> ToolRegistry:
    spec = ToolSpec(
        name=_SALES_TOOL_NAME,
        description="测试销售订单查询工具：固定返回 SO000001",
        parameters=_CustomerParams.model_json_schema(),
    )
    registry = ToolRegistry()
    registry.register(
        _SalesTool(
            spec=spec,
            params_model=_CustomerParams,
            build_filter=lambda _p: FilterExpression(),
            endpoint=SAL_ORDER,
        )
    )
    return registry


_ACTION_TOOL_NAME = "erp_unaudit_sales_order"


class _BillActionParams(EndpointParams):
    """单据动作假参数：只需单号。"""

    bill_no: str = ""


class _ActionTool(ToolDefinition):
    """FakeTool：写动作工具，固定返回成功（不碰金蝶 client）。"""

    async def execute(
        self, client: Any, arguments: dict[str, Any], *, row_filter: FilterExpression | None = None
    ) -> ActionResult:
        return ActionResult(
            success=True,
            form_id="SAL_Order",
            bill_no=str(arguments.get("bill_no", "")),
        )


def _make_action_registry() -> ToolRegistry:
    spec = ToolSpec(
        name=_ACTION_TOOL_NAME,
        description="测试反审核工具：固定返回成功",
        parameters=_BillActionParams.model_json_schema(),
    )
    registry = ToolRegistry()
    registry.register(
        _ActionTool(
            spec=spec,
            params_model=_BillActionParams,
            build_filter=lambda _p: FilterExpression(),
            endpoint=SAL_ORDER,
            tool_type="action",
            action="unaudit",
        )
    )
    return registry


_PURCHASE_AUDIT_TOOL_NAME = "erp_audit_purchase_order"
_KB_TOOL_NAME = "kb_search"


def _make_planning_registry() -> ToolRegistry:
    """查询 + 采购审核写工具 + kb_search，用于覆盖/补全测试。"""
    registry = _make_sales_registry()
    registry.register(
        _ActionTool(
            spec=ToolSpec(
                name=_PURCHASE_AUDIT_TOOL_NAME,
                description="测试采购审核工具：固定返回成功",
                parameters=_BillActionParams.model_json_schema(),
            ),
            params_model=_BillActionParams,
            build_filter=lambda _p: FilterExpression(),
            endpoint=SAL_ORDER,
            tool_type="write",
            action="audit",
        )
    )
    registry.register(
        _EchoTool(
            spec=ToolSpec(
                name=_KB_TOOL_NAME,
                description="测试知识检索工具：固定返回一行",
                parameters=_CustomerParams.model_json_schema(),
            ),
            params_model=_CustomerParams,
            build_filter=lambda _p: FilterExpression(),
            endpoint=SAL_ORDER,
        )
    )
    return registry


def _settings(max_steps: int = 5) -> Settings:
    # _env_file=None：防本机 .env 的 KINGPILOT_* 污染（T03 教训：env_file 独立于 os.environ）
    return Settings(_env_file=None, agent_max_steps=max_steps)


def _plan(*tool_names: str) -> PlanResult:
    return PlanResult(
        tool_calls=[ToolCallDecision(tool_name=name, arguments={}) for name in tool_names]
    )


def _continue(next_input: str) -> ReflectionResult:
    return ReflectionResult(continue_execution=True, next_input=next_input)


def _stop() -> ReflectionResult:
    return ReflectionResult(continue_execution=False)


async def test_chat_single_turn_walks_all_phases() -> None:
    """纯聊天单轮：plan 空 → 无可观察结果不反思 → 走通全 Phase。"""
    provider = FakeProvider(
        plan_queue=[_plan()], reflection_queue=[_stop()], response_queue=["你好"]
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("你好")

    assert response.content == "你好"
    assert provider.plan_calls == 1
    # M3-T05：纯聊天无工具执行，不调反思（省一次 LLM 调用），reflections 不虚增
    assert provider.reflect_calls == 0
    assert provider.generate_calls == 1
    trace = response.trace
    assert trace is not None
    assert trace.reflections == []
    assert trace.used_fallback is False
    assert trace.final_response == "你好"
    assert trace.total_latency_ms >= 0.0


async def test_empty_plan_is_completed_by_rule_engine_for_explicit_code() -> None:
    """LLM 未调工具但用户给出明确单号：规则引擎补全只读查询。"""
    provider = FakeProvider(
        plan_queue=[PlanResult(tool_calls=[])],
        response_queue=["查到了 SO000001"],
    )
    core = AgentCore(provider, _make_sales_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查一下销售订单 SO000001")

    trace = response.trace
    assert trace is not None
    assert len(trace.tool_executions) == 1
    assert trace.tool_executions[0].tool_name == _SALES_TOOL_NAME
    assert trace.llm_planning["rule_fallback"] == {
        "intent": "sales_order",
        "reason": "llm_plan_empty",
    }
    assert trace.llm_planning["tool_calls"][0]["tool_name"] == _SALES_TOOL_NAME
    assert trace.used_fallback is False


async def test_empty_plan_is_completed_by_rule_engine_for_write_intent() -> None:
    """LLM 漏掉写工具时，「动作 + 单号」补全为真实写调用，不再只给一句答复。"""
    provider = FakeProvider(
        plan_queue=[PlanResult(tool_calls=[])],
        response_queue=["已提交审批"],
    )
    core = AgentCore(provider, _make_action_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("反审核销售订单 SO900001")

    trace = response.trace
    assert trace is not None
    assert [execution.tool_name for execution in trace.tool_executions] == [_ACTION_TOOL_NAME]
    assert trace.tool_executions[0].parameters == {"bill_no": "SO900001"}
    assert trace.tool_executions[0].error is None
    assert trace.llm_planning["rule_fallback"] == {
        "intent": "write_unaudit",
        "reason": "llm_plan_empty",
    }


async def test_explicit_write_action_overrides_read_only_plan() -> None:
    """LLM 把「审核采购订单」规划成查询时，规则引擎纠正为写工具。"""
    provider = FakeProvider(
        plan_queue=[_plan(_SALES_TOOL_NAME)],
        response_queue=["已提交审批"],
    )
    core = AgentCore(provider, _make_planning_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("审核采购订单 PO900001")

    trace = response.trace
    assert trace is not None
    assert [execution.tool_name for execution in trace.tool_executions] == [
        _PURCHASE_AUDIT_TOOL_NAME
    ]
    assert trace.tool_executions[0].parameters == {"bill_no": "PO900001"}
    assert trace.llm_planning["rule_override"] == {
        "intent": "write_audit",
        "reason": "explicit_write_action",
    }


async def test_question_about_write_action_is_not_overridden() -> None:
    """带疑问词的咨询句不能被覆盖成写操作。"""
    provider = FakeProvider(
        plan_queue=[_plan(_SALES_TOOL_NAME)],
        response_queue=["需要具备审核权限"],
    )
    core = AgentCore(provider, _make_planning_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("审核采购订单 PO900001 需要什么条件？")

    trace = response.trace
    assert trace is not None
    assert "rule_override" not in trace.llm_planning
    assert trace.tool_executions[0].tool_name == _SALES_TOOL_NAME


async def test_empty_plan_knowledge_question_calls_kb_search() -> None:
    """知识意图且 LLM 未调工具时，补 kb_search，避免无引用作答。"""
    provider = FakeProvider(
        plan_queue=[PlanResult(tool_calls=[])],
        response_queue=["见知识库"],
    )
    core = AgentCore(provider, _make_planning_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("快递一直不更新怎么办？")

    trace = response.trace
    assert trace is not None
    assert trace.tool_executions[0].tool_name == _KB_TOOL_NAME
    assert trace.llm_planning["rule_fallback"] == {
        "intent": "knowledge",
        "reason": "knowledge_intent",
    }


async def test_empty_plan_without_explicit_code_stays_chat() -> None:
    """无明确业务编码时不补全，纯聊天行为保持不变。"""
    provider = FakeProvider(plan_queue=[_plan()], response_queue=["你好"])
    core = AgentCore(provider, _make_sales_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("你好")

    trace = response.trace
    assert trace is not None
    assert trace.tool_executions == []
    assert "rule_fallback" not in trace.llm_planning


async def test_single_tool_turn_executes_and_records() -> None:
    """单工具一轮：Phase 3 真执行 echo 工具，result 进 trace。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["查到了"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查一下")

    trace = response.trace
    assert trace is not None
    assert len(trace.tool_executions) == 1
    execution = trace.tool_executions[0]
    assert execution.tool_name == _ECHO_TOOL_NAME
    assert execution.error is None
    assert execution.result["rows"] == [{"echo": "ok"}]
    assert trace.llm_planning["tool_calls"][0]["tool_name"] == _ECHO_TOOL_NAME


async def test_two_step_orchestration_via_reflect() -> None:
    """两步编排：reflect 续跑一次 → plan_calls==2，next_input 与观察回灌进 context。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME), _plan()],
        reflection_queue=[_continue("再查它的订单"), _stop()],
        response_queue=["完成"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("先查客户")

    assert provider.plan_calls == 2
    # 第二步 plan 未调工具（模型决定直接答复）→ 无可观察结果，不再反思
    assert provider.reflect_calls == 1
    trace = response.trace
    assert trace is not None
    assert len(trace.reflections) == 1
    assert trace.reflections[0]["continue_execution"] is True
    assert trace.reflections[0]["next_input"] == "再查它的订单"
    assert trace.used_fallback is False
    # 第二步规划上下文：上一步工具结果（观察回灌）+ 续跑指令
    context = provider.last_plan_context
    assert context is not None
    assert context.messages[-1].content == "再查它的订单"
    assert any("工具结果" in m.content and '"echo": "ok"' in m.content for m in context.messages)


async def test_max_steps_circuit_breaker_returns_partial() -> None:
    """熔断：reflect 永远续跑 → 循环在 max_steps 停，给部分结果不抛错。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)] * 3,  # max_steps=3 → 3 轮
        reflection_queue=[_continue("继续1"), _continue("继续2"), _continue("继续3")],
        response_queue=["部分结果"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings(max_steps=3))  # type: ignore[arg-type]

    response = await core.run("一直跑")

    assert provider.plan_calls == 3  # 恰好触顶
    assert provider.reflect_calls == 3
    trace = response.trace
    assert trace is not None
    assert trace.used_fallback is True  # 触顶停步标记
    assert response.content == "部分结果"  # 仍返回部分结果，不抛错


async def test_trace_threading_and_fields() -> None:
    """trace 贯穿：trace_id 一致、reflections 累积、耗时记录。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["ok"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查")

    trace = response.trace
    assert trace is not None
    assert response.trace_id == trace.trace_id
    assert trace.user_input == "查"
    assert trace.user_id == "local_dev"
    assert len(trace.reflections) == 1
    assert trace.total_latency_ms >= 0.0


async def test_token_usage_sink_channel() -> None:
    """规范 4：usage_sink 通道接通，每次 LLM 调用上报一次（plan/reflect/generate）。"""
    captured: list[TokenUsage] = []
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["ok"],
        usage_sink=captured.append,
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    await core.run("查")

    # plan + reflect + generate_response 各上报一次
    assert len(captured) == 3
    assert all(u.total_tokens > 0 for u in captured)


# ── M3-T03：记忆接线 ──


async def test_memory_written_back_and_reflect_gets_snapshot() -> None:
    """有 memory：单轮写回 user/tool/assistant；reflect 收到真实（空）快照。"""
    memory = MemoryStore(session_id="sess-1")
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["最终答复"],
    )
    # echo tool 覆盖 execute，不碰 client（T01 既有模式）
    core = AgentCore(
        provider,
        _make_echo_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    response = await core.run("查一下")

    assert memory.raw_rounds == 1
    messages = memory.raw_messages()
    assert [m.role for m in messages] == ["user", "tool", "assistant"]
    assert messages[0].content == "查一下"
    assert messages[1].structured_result["rows"] == [{"echo": "ok"}]
    assert messages[2].content == "最终答复"
    # Phase 0 取快照在本轮写回之前——reflect 看到的 L1 为空（历史态）
    assert provider.last_reflect_memory is not None
    assert provider.last_reflect_memory.layer1_recent == []
    assert response.trace is not None
    assert response.trace.session_id == "sess-1"


async def test_phase0_compresses_long_history_before_planning() -> None:
    """预置 8 轮：Phase 0 先压缩再组装——plan 看到 system 摘要+6 轮历史+当前输入。"""
    memory = MemoryStore(session_id="sess-2")
    for i in range(1, 9):
        memory.add_raw("user", f"历史问题{i}")
        memory.add_raw("assistant", f"历史回复{i}")
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=["新答复"],
        summarize_queue=[SummaryDraft(summary_text="前两轮历史压缩摘要", key_entities=[])],
    )
    # echo tool 覆盖 execute，不碰 client（T01 既有模式）
    core = AgentCore(
        provider,
        _make_echo_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    await core.run("新问题")

    # Phase 0 压缩：8 轮压掉前 2 轮；Phase 6 写回本轮 → 7 轮
    assert provider.summarize_calls == 1
    assert memory.raw_rounds == 7
    context = provider.last_plan_context
    assert context is not None
    assert context.messages[0].role == "system"
    assert "前两轮历史压缩摘要" in context.messages[0].content
    assert context.messages[-1].content == "新问题"
    assembled = " ".join(m.content for m in context.messages)
    assert "历史问题1" not in assembled  # 被压缩轮的原文不进上下文
    assert "历史问题7" in assembled  # 保留段（相对第 6 轮）在
    # 本轮写回后最后两条为 user/assistant
    assert memory.raw_messages()[-1].content == "新答复"
    # assistant 消息带 Trace 链接（控制台历史「查看 Trace」按钮的数据来源）
    assert memory.raw_messages()[-1].trace_id


# ── M3-T04：实体图抽取与锚点注入 ──


async def test_tool_result_entities_extracted_into_graph() -> None:
    """客户查询工具结果 → CUS001 入实体图（Phase 6 抽取接线）。"""
    memory = MemoryStore(session_id="sess-entity")
    provider = FakeProvider(
        plan_queue=[_plan(_CUSTOMER_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["查到了客户"],
    )
    core = AgentCore(
        provider,
        _make_customer_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    await core.run("查测试客户")

    customers = memory.entities("customer")
    assert len(customers) == 1
    assert customers[0].kingdee_id == "CUS001"
    assert customers[0].display_name == "测试客户"
    # last_mentioned = 本轮号（user 写入后 = 1）
    assert customers[0].last_mentioned_in_round == 1


async def test_entity_anchors_injected_into_plan_context() -> None:
    """Phase 0 把实体图渲染为 anchors 注入 plan 上下文（LLM 可见）。"""
    memory = MemoryStore(session_id="sess-anchors")
    # 预置一个客户实体
    memory.upsert_entity(
        EntityRef(
            entity_type="customer",
            display_name="预置客户",
            kingdee_id="CUS_PRE",
            last_mentioned_in_round=1,
        )
    )
    provider = FakeProvider(plan_queue=[_plan()], reflection_queue=[_stop()], response_queue=["ok"])
    # echo tool 覆盖 execute，不碰 client（T01 既有模式）
    core = AgentCore(
        provider,
        _make_echo_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    await core.run("你好")

    context = provider.last_plan_context
    assert context is not None
    assert "customer:预置客户(CUS_PRE)" in context.anchors


async def test_previous_customer_answerable_across_rounds() -> None:
    """验收核心：第 1 轮查客户 → 实体入图；第 2 轮 plan 上下文含该客户锚点。"""
    memory = MemoryStore(session_id="sess-multi")
    registry = _make_customer_registry()

    # 第 1 轮：查客户
    provider1 = FakeProvider(
        plan_queue=[_plan(_CUSTOMER_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["查到 CUS001"],
    )
    core1 = AgentCore(provider1, registry, client=None, settings=_settings(), memory=memory)  # type: ignore[arg-type]
    await core1.run("查测试客户")

    assert memory.find_entity("customer", kingdee_id="CUS001") is not None

    # 第 2 轮：plan 上下文必须含上一轮的客户锚点（可回答「之前提到的客户」）
    provider2 = FakeProvider(
        plan_queue=[_plan()], reflection_queue=[_stop()], response_queue=["好的"]
    )
    core2 = AgentCore(provider2, registry, client=None, settings=_settings(), memory=memory)  # type: ignore[arg-type]
    await core2.run("之前那个客户的订单呢")

    context2 = provider2.last_plan_context
    assert context2 is not None
    assert any("CUS001" in a for a in context2.anchors)


# ── M3-T05：反思层续跑防御 ──


async def test_blank_next_input_stops_multi_step_loop() -> None:
    """reflect 说续跑但指令为空 → 归一为停止，不再多跑一轮。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME), _plan(_ECHO_TOOL_NAME)],
        reflection_queue=[ReflectionResult(continue_execution=True, next_input="   ")],
        response_queue=["完成"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查")

    assert provider.plan_calls == 1
    assert provider.reflect_calls == 1
    trace = response.trace
    assert trace is not None
    assert trace.reflections[0]["continue_execution"] is False
    assert "续跑指令为空" in trace.reflections[0]["reasoning"]


async def test_duplicate_next_input_stops_loop_and_reflects_only_step_executions() -> None:
    """连续两轮相同续跑指令 → 第二轮归一为停止；反思只收本步执行（非累积）。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME), _plan(_ECHO_TOOL_NAME), _plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_continue("再查订单"), _continue("再查订单")],
        response_queue=["完成"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查")

    assert provider.plan_calls == 2
    trace = response.trace
    assert trace is not None
    assert len(trace.reflections) == 2
    assert trace.reflections[1]["continue_execution"] is False
    assert "重复" in trace.reflections[1]["reasoning"]
    # 第二步反思只收到本步 1 条执行，而非累积的 2 条
    assert provider.last_reflect_summary is not None
    assert len(provider.last_reflect_summary.tool_executions) == 1


# ── M3-T06：幻觉检测接线 ──


async def test_phase5_flags_numeric_hallucination() -> None:
    """回复声称 5 张订单，工具实际只返回 1 行 → 检出并告警。"""
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["查到 5 张订单"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查")

    trace = response.trace
    assert trace is not None
    assert trace.hallucination_detected is True
    assert trace.hallucination_resolved is False  # 纠错闭环归 M4
    assert response.hallucination_warning is True


async def test_phase5_clean_response_not_flagged() -> None:
    provider = FakeProvider(
        plan_queue=[_plan(_ECHO_TOOL_NAME)],
        reflection_queue=[_stop()],
        response_queue=["已查到数据"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("查")

    trace = response.trace
    assert trace is not None
    assert trace.hallucination_detected is False
    assert response.hallucination_warning is False


async def test_phase5_uses_entity_graph_for_status_claim() -> None:
    """实体图记录 SO000008 已审核，回复声称暂存 → 业务一致性命中。"""
    memory = MemoryStore(session_id="sess-hallucination")
    memory.upsert_entity(
        EntityRef(
            entity_type="order",
            display_name="SO000008",
            kingdee_id="SO000008",
            last_mentioned_in_round=1,
            attributes={"FDocumentStatus": "C"},
        )
    )
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=["订单 SO000008 是暂存状态"],
    )
    # echo tool 覆盖 execute，不碰 client（T01 既有模式）
    core = AgentCore(
        provider,
        _make_echo_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    response = await core.run("订单 SO000008 什么状态")

    trace = response.trace
    assert trace is not None
    assert trace.hallucination_detected is True
    assert response.hallucination_warning is True


async def test_phase5_uses_session_history_for_cross_turn_reference() -> None:
    """多轮指代：上一轮查到的 CUS001/订单金额在本轮引用不应误报。"""
    memory = MemoryStore(session_id="sess-cross-turn")
    memory.add_raw("user", "查客户 CUS001 的订单")
    memory.add_raw(
        "tool",
        "",
        structured_result={
            "form_id": "SAL_Order",
            "rows": [{"FBillNo": "SO000012", "FBillAllAmount": "181736.0000000000"}],
        },
    )
    memory.add_raw("assistant", "查到一笔订单")
    provider = FakeProvider(
        plan_queue=[_plan()],
        reflection_queue=[_stop()],
        response_queue=["客户 CUS001 的订单 SO000012 金额为 181,736.00 元"],
    )
    # echo tool 覆盖 execute，不碰 client（T01 既有模式）
    core = AgentCore(
        provider,
        _make_echo_registry(),
        client=None,  # type: ignore[arg-type]
        settings=_settings(),
        memory=memory,
    )

    response = await core.run("它多少钱")

    trace = response.trace
    assert trace is not None
    assert trace.hallucination_detected is False
    assert response.hallucination_warning is False


async def test_empty_generation_is_retried_once() -> None:
    """LLM 偶发空答复：重试一次并采用第二次结果。"""
    provider = FakeProvider(
        plan_queue=[_plan()],
        response_queue=["", "第二次答复"],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("你好")

    assert response.content == "第二次答复"
    assert provider.generate_calls == 2


async def test_empty_generation_falls_back_after_retry() -> None:
    """两次都为空时返回可读兜底文案，避免前端收到空 final。"""
    provider = FakeProvider(
        plan_queue=[_plan()],
        response_queue=["", "   "],
    )
    core = AgentCore(provider, _make_echo_registry(), client=None, settings=_settings())  # type: ignore[arg-type]

    response = await core.run("你好")

    assert response.content == "抱歉，本次没有生成有效答复，请重试或换个问法。"
    assert provider.generate_calls == 2
