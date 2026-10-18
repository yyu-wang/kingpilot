"""M3-T07：CLI 对话循环单测（FakeProvider + tmp_path，零网络零终端）。

覆盖：5 轮对话每轮落盘、进程重启后历史续聊（指代理解的数据基础）、
单轮失败不终结会话、幻觉告警提示、/exit 立即退出、--list 列表。
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from agent.cli import main, run_chat_loop
from agent.core import AgentCore
from agent.memory import MemoryStore
from agent.session_store import SessionStore
from agent.tools import ToolRegistry
from config.settings import Settings
from infrastructure.exceptions import LLMUnavailableError
from models.llm import PlanResult
from tests.fakes import FakeProvider


def _reader(inputs: list[str]) -> Callable[[str], str]:
    """脚本化输入：耗尽后抛 EOFError（模拟 Ctrl+D）。"""
    iterator = iter(inputs)

    def read(_prompt: str) -> str:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise EOFError from exc

    return read


def _settings(tmp_path: Path) -> Settings:
    # _env_file=None：防止本机 .env 的会话目录/阈值污染测试
    return Settings(_env_file=None, session_store_dir=tmp_path)


def _core(provider: FakeProvider, memory: MemoryStore, settings: Settings) -> AgentCore:
    # 空 registry：这些用例只验证 CLI 循环与持久化，不触工具（与 T01 无状态路径一致）
    return AgentCore(
        provider,
        ToolRegistry(),
        client=None,  # type: ignore[arg-type]
        settings=settings,
        memory=memory,
    )


async def test_five_turn_loop_persists_each_turn(tmp_path: Path) -> None:
    """连续 5 轮：内存累积 5 轮且磁盘文件同步可还原（验收的持久化基础）。"""
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path)
    memory = store.load("cli-5")
    provider = FakeProvider(
        plan_queue=[PlanResult(tool_calls=[])] * 5,
        response_queue=[f"第{i}轮答复" for i in range(1, 6)],
    )
    outputs: list[str] = []

    await run_chat_loop(
        _core(provider, memory, settings),
        memory,
        store,
        read_input=_reader(["第一问", "第二问", "第三问", "第四问", "第五问", "/exit"]),
        write_output=outputs.append,
    )

    assert sum("助手>" in line for line in outputs) == 5
    assert memory.raw_rounds == 5
    reloaded = store.load("cli-5")
    assert reloaded.raw_rounds == 5
    assert reloaded.raw_messages()[0].content == "第一问"
    assert reloaded.raw_messages()[-1].content == "第5轮答复"
    assert any("会话已保存" in line for line in outputs)


async def test_restart_resumes_history_for_next_plan(tmp_path: Path) -> None:
    """进程重启后从磁盘恢复 5 轮 → 第 6 轮 plan 上下文包含历史与当前输入。"""
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path)
    first_memory = store.load("cli-restart")
    first_provider = FakeProvider(
        plan_queue=[PlanResult(tool_calls=[])] * 2,
        response_queue=["答复一", "答复二"],
    )
    await run_chat_loop(
        _core(first_provider, first_memory, settings),
        first_memory,
        store,
        read_input=_reader(["记住客户 CUS001", "它的订单一共几笔", "/exit"]),
        write_output=lambda _line: None,
    )

    # 模拟重启：新建 MemoryStore 从磁盘加载，再跑第 3 轮
    restored = store.load("cli-restart")
    assert restored.raw_rounds == 2
    provider = FakeProvider(plan_queue=[PlanResult(tool_calls=[])], response_queue=["它有两笔订单"])
    await run_chat_loop(
        _core(provider, restored, settings),
        restored,
        store,
        read_input=_reader(["它是已审核的吗", "/exit"]),
        write_output=lambda _line: None,
    )

    context = provider.last_plan_context
    assert context is not None
    assembled = " ".join(message.content for message in context.messages)
    assert "记住客户 CUS001" in assembled  # 重启前的历史仍在
    assert "它的订单一共几笔" in assembled
    assert context.messages[-1].content == "它是已审核的吗"
    assert restored.raw_rounds == 3


async def test_single_turn_failure_keeps_session_alive(tmp_path: Path) -> None:
    """首轮 LLM 不可用 → 走只读降级提示但会话继续，下一轮正常并落盘。"""
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path)
    memory = store.load("cli-error")
    provider = FakeProvider(
        plan_queue=[LLMUnavailableError("模拟超时"), PlanResult(tool_calls=[])],
        response_queue=["恢复正常"],
    )
    outputs: list[str] = []

    await run_chat_loop(
        _core(provider, memory, settings),
        memory,
        store,
        read_input=_reader(["会失败的问题", "再试一次", "/exit"]),
        write_output=outputs.append,
    )

    assert any("LLM 暂不可用" in line for line in outputs)
    assert any("助手> 恢复正常" in line for line in outputs)
    # 降级轮也会写回记忆（不再静默丢轮次）
    assert memory.raw_rounds == 2
    assert store.load("cli-error").raw_rounds == 2


async def test_hallucination_warning_surfaced(tmp_path: Path) -> None:
    """检测到幻觉时 CLI 显性提示（衔接 M3-T06），会话不中断。"""
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path)
    memory = store.load("cli-hallucination")
    provider = FakeProvider(response_queue=["查到 5 张订单"])
    outputs: list[str] = []

    await run_chat_loop(
        _core(provider, memory, settings),
        memory,
        store,
        read_input=_reader(["查到几笔", "/exit"]),
        write_output=outputs.append,
    )

    assert any("未能与工具原始结果直接核对" in line for line in outputs)
    assert memory.raw_rounds == 1


async def test_exit_command_stops_without_running_turn(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path)
    memory = store.load("cli-exit")
    provider = FakeProvider()

    await run_chat_loop(
        _core(provider, memory, settings),
        memory,
        store,
        read_input=_reader(["/exit", "不应执行"]),
        write_output=lambda _line: None,
    )

    assert provider.plan_calls == 0
    assert memory.raw_rounds == 0


def test_main_list_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path)
    memory = store.load("sess-x")
    memory.add_raw("user", "问题")
    memory.add_raw("assistant", "答复")
    store.save(memory)

    assert main(["--list"], settings=settings) == 0

    captured = capsys.readouterr().out
    assert "sess-x" in captured
    assert "1 轮" in captured


def test_main_reports_corrupt_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "bad.json").write_text("{broken", encoding="utf-8")

    assert main(["--session", "bad"], settings=settings) == 1

    assert "会话加载失败" in capsys.readouterr().out
