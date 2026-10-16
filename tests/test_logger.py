"""日志基建单测（M0-T05 验收）。

验收点：两次不同 trace 的日志各自携带正确 trace_id。
另覆盖：默认占位、上下文还原、并发隔离、stdlib 拦截、幂等装配。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest
from loguru import logger

from infrastructure.logger import (
    DEFAULT_TRACE_ID,
    LOG_FORMAT,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    setup_logging,
    with_trace,
)


class _ListSink:
    """测试 sink：loguru 已按 format 渲染好，这里只需收集文本。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, message: str) -> None:
        self.lines.append(message)


@pytest.fixture(autouse=True)
def _clean_logging_state() -> Iterator[None]:
    """用例前后清理：trace 复位 + 卸载全部 sink（避免残留指向失效的捕获流）。"""
    reset_trace_id()
    yield
    logger.remove()
    reset_trace_id()


@pytest.fixture
def sink() -> Iterator[_ListSink]:
    """先按正式配置装配，再挂一个捕获 sink。"""
    setup_logging()
    collector = _ListSink()
    handler_id = logger.add(collector.write, format=LOG_FORMAT, level="DEBUG")
    yield collector
    logger.remove(handler_id)


class TestTraceInjection:
    """验收核心：trace_id 逐条正确注入。"""

    def test_two_traces_carry_own_id(self, sink: _ListSink) -> None:
        set_trace_id("trace-A")
        logger.info("第一条")
        set_trace_id("trace-B")
        logger.info("第二条")

        assert "[trace-A]" in sink.lines[0]
        assert "[trace-B]" not in sink.lines[0]
        assert "[trace-B]" in sink.lines[1]
        assert "[trace-A]" not in sink.lines[1]

    def test_no_cross_call_leak(self, sink: _ListSink) -> None:
        """防回归：第 N 条不得复用第 N-1 条的 trace_id。"""
        set_trace_id("first")
        logger.info("a")
        set_trace_id("second")
        logger.info("b")

        assert "first" not in sink.lines[1]

    def test_unset_shows_placeholder(self, sink: _ListSink) -> None:
        logger.info("无 trace")
        assert f"[{DEFAULT_TRACE_ID}]" in sink.lines[0]


class TestTraceApi:
    """trace_id 读写与上下文管理。"""

    def test_new_trace_id_is_unique_hex(self) -> None:
        first, second = new_trace_id(), new_trace_id()
        assert first != second
        assert len(first) == 32
        int(first, 16)  # 非法十六进制会抛 ValueError

    def test_with_trace_generates_when_absent(self) -> None:
        with with_trace() as tid:
            assert tid != DEFAULT_TRACE_ID
            assert get_trace_id() == tid

    def test_with_trace_uses_given_id(self) -> None:
        with with_trace("fixed-id") as tid:
            assert tid == "fixed-id"
            assert get_trace_id() == "fixed-id"

    def test_with_trace_restores_on_exit(self) -> None:
        with with_trace("outer"):
            assert get_trace_id() == "outer"
        assert get_trace_id() == DEFAULT_TRACE_ID

    def test_nested_with_trace_restores_outer(self) -> None:
        with with_trace("outer"):
            with with_trace("inner"):
                assert get_trace_id() == "inner"
            assert get_trace_id() == "outer"

    def test_with_trace_restores_on_exception(self) -> None:
        with pytest.raises(RuntimeError), with_trace("boom"):
            raise RuntimeError("故意失败")
        assert get_trace_id() == DEFAULT_TRACE_ID


class TestAsyncIsolation:
    """contextvar 在并发任务间天然隔离（每 task 拷贝上下文）。"""

    async def test_concurrent_tasks_do_not_mix(self, sink: _ListSink) -> None:
        async def worker(tid: str) -> None:
            set_trace_id(tid)
            await asyncio.sleep(0)  # 让出控制权，制造交错执行
            logger.info(f"work-{tid}")

        await asyncio.gather(worker("t-1"), worker("t-2"))

        line_1 = next(ln for ln in sink.lines if "work-t-1" in ln)
        line_2 = next(ln for ln in sink.lines if "work-t-2" in ln)
        assert "[t-1]" in line_1
        assert "[t-2]" not in line_1
        assert "[t-2]" in line_2
        assert "[t-1]" not in line_2


class TestStdlibInterception:
    """第三方库的 stdlib 日志统一汇入 loguru。"""

    def test_stdlib_record_forwarded(self, sink: _ListSink) -> None:
        logging.getLogger("httpx._client").warning("来自 stdlib 的日志")
        assert any("来自 stdlib 的日志" in ln for ln in sink.lines)

    def test_stdlib_level_preserved(self, sink: _ListSink) -> None:
        logging.getLogger("some.third_party").error("错误级别消息")
        line = next(ln for ln in sink.lines if "错误级别消息" in ln)
        assert "ERROR" in line

    def test_reports_real_caller_frame(self, sink: _ListSink) -> None:
        """回归：调用位置须指向真实调用方，而非 logging/拦截器内部帧。"""
        logging.getLogger("httpx._client").warning("定位校验")
        line = next(ln for ln in sink.lines if "定位校验" in ln)
        assert "test_logger" in line
        assert "callHandlers" not in line
        assert "logger.py" not in line

    def test_stdlib_unknown_level_falls_back_to_levelno(self, sink: _ListSink) -> None:
        """回归 M1-T07：loguru 不认识的级别名（自定义值 35 → "Level 35"）走 levelno 兜底。

        ``logger.level("Level 35")`` 抛 ValueError，拦截器须降级用 ``record.levelno``
        继续转发——该分支此前无任何用例触达（覆盖率自查缺口）。
        """
        logging.log(35, "未知级别的兜底消息")
        line = next(ln for ln in sink.lines if "未知级别的兜底消息" in ln)
        assert "35" in line


class TestSetupLogging:
    """装配行为。"""

    def test_idempotent_no_duplicate_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        setup_logging()
        setup_logging()  # 第二次不应叠加 handler
        logger.info("唯一一行")
        assert capsys.readouterr().err.count("唯一一行") == 1

    def test_format_renders_trace_id(self) -> None:
        # 坑点固化：format 不含 {extra[trace_id]} 则注入了也不显示
        assert "{extra[trace_id]}" in LOG_FORMAT
