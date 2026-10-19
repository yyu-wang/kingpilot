"""M9-T02：AgentCore 事件旁路单测。"""

from collections.abc import Callable

from agent.core import AgentCore
from agent.events import AgentEvent
from agent.tools import build_default_registry
from config.settings import Settings
from kingdee.client import KingdeeClient
from models.llm import PlanResult, ToolCallDecision
from models.user import UserContext
from tests.fakes import FakeProvider

_ALICE = UserContext(user_id="alice", role="viewer")


def _core(
    provider: FakeProvider,
    client: KingdeeClient,
    sink: Callable[[AgentEvent], None] | None,
) -> AgentCore:
    return AgentCore(
        provider,
        build_default_registry(),
        client,
        Settings(_env_file=None),
        event_sink=sink,
    )


async def test_event_sequence_for_tool_query(client: KingdeeClient) -> None:
    events: list[AgentEvent] = []
    provider = FakeProvider(
        plan_queue=[
            PlanResult(
                tool_calls=[
                    ToolCallDecision(
                        tool_name="kingdee_query_sales_order",
                        arguments={"bill_no": "SO000001"},
                    )
                ]
            )
        ],
        response_queue=["查询完成"],
    )
    core = _core(provider, client, events.append)

    response = await core.run("查询销售订单 SO000001", _ALICE)

    assert response.content == "查询完成"
    assert response.trace is not None
    assert response.trace.tool_executions[0].error is None, response.trace.tool_executions[0].error
    phases = [(event.phase, event.status) for event in events if event.event == "phase"]
    assert phases == [
        ("plan", "start"),
        ("plan", "success"),
        ("guard", "start"),
        ("guard", "success"),
        ("execute", "start"),
        ("execute", "success"),
        ("reflect", "start"),
        ("reflect", "success"),
        ("generate", "start"),
        ("generate", "success"),
    ]

    tool_events = [event for event in events if event.event == "tool"]
    assert [event.tool_name for event in tool_events] == [
        "kingdee_query_sales_order",
        "kingdee_query_sales_order",
    ]
    assert [event.status for event in tool_events] == ["start", "success"]
    for event in tool_events:
        payload = event.model_dump(exclude_defaults=True)
        assert set(payload) <= {"event", "tool_name", "status", "latency_ms"}


async def test_event_sink_failure_does_not_break_run(client: KingdeeClient) -> None:
    def broken_sink(event: AgentEvent) -> None:
        raise RuntimeError("sink down")

    provider = FakeProvider(response_queue=["ok"])
    core = _core(provider, client, broken_sink)

    response = await core.run("你好", _ALICE)

    assert response.content == "ok"
