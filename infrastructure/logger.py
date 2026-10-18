"""日志基建（M0-T05）。

职责：
1. 统一 loguru 出口：全项目只用 ``from loguru import logger``，禁止裸 print。
2. ``trace_id`` 贯穿：contextvar 存储，patcher 逐条注入，format 渲染。
3. 收编 stdlib logging：第三方库（httpx 等）日志统一汇入 loguru。

用法::

    setup_logging()                      # 进程启动时调一次（幂等）
    with with_trace() as tid:            # 一次请求 / 一次 Agent 运行
        logger.info("处理开始")           # 自动带 [tid]

实现要点（M0-T05 实测）：
    patcher 只负责注入 ``extra["trace_id"]``；format **必须显式包含**
    ``{extra[trace_id]}`` 才会被渲染 —— 否则注入了也不显示。
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import FrameType
from typing import TYPE_CHECKING

from loguru import logger

from config.settings import get_settings
from safety.datasecurity import redact_text

if TYPE_CHECKING:
    # loguru 的 __init__.pyi 仅在类型检查期可见（运行期只有 logger）
    from loguru import Record

#: 未进入 trace 时显示的占位符
DEFAULT_TRACE_ID = "-"

#: 统一日志格式：时间 | 级别 | [trace_id] | 模块:函数:行号 - 消息
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | [{extra[trace_id]}] | {name}:{function}:{line} - {message}"

#: 当前 trace 上下文。default 保证"没设过"时也有值，format 不会 KeyError。
_trace_id: ContextVar[str] = ContextVar("kingpilot_trace_id", default=DEFAULT_TRACE_ID)


# ────────────────────────── trace_id 读写 ──────────────────────────


def new_trace_id() -> str:
    """生成一个新的 trace_id（32 位十六进制）。"""
    return uuid.uuid4().hex


def get_trace_id() -> str:
    """取当前上下文的 trace_id（未设置则为占位符 ``-``）。"""
    return _trace_id.get()


def set_trace_id(trace_id: str) -> None:
    """设置当前上下文的 trace_id（不回滚，慎用；优先用 ``with_trace``）。"""
    _trace_id.set(trace_id)


def reset_trace_id() -> None:
    """复位为占位符（测试/清理用）。"""
    _trace_id.set(DEFAULT_TRACE_ID)


@contextmanager
def with_trace(trace_id: str | None = None) -> Iterator[str]:
    """trace 上下文管理器：进入时设置，退出时**自动还原**（基于 token）。

    不传 ``trace_id`` 时自动生成，并 yield 出去供调用方记录。
    """
    tid = trace_id or new_trace_id()
    token = _trace_id.set(tid)
    try:
        yield tid
    finally:
        _trace_id.reset(token)


# ────────────────────────── loguru 装配 ──────────────────────────


def _inject_trace_id(record: Record) -> None:
    """patcher：注入 trace_id，并对日志消息做 PII 掩码（M7-T01 日志出口）。

    ``record["extra"]`` 是 copy，故逐条覆盖不会串味（M0-T05 已用并发用例验证）。
    """
    record["extra"]["trace_id"] = _trace_id.get()
    record["message"] = redact_text(record["message"])


class InterceptHandler(logging.Handler):
    """把 stdlib ``logging`` 记录转发到 loguru。

    ``depth`` 需同时跳过两类帧，才能定位到**真实调用方**：
    1. stdlib logging 内部帧（``Logger.warning`` / ``callHandlers`` / ``handle`` …）
    2. 本处理器自身的帧

    注：loguru 官方示例只跳过第 1 类，因 ``logging.currentframe()`` 返回的是
    ``emit`` 自身的帧（在本文件），循环原地不动 —— 实测会把调用位置错报成
    ``logging:callHandlers``。故这里额外排除 ``__file__``（M0-T05 实测修正）。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame: FrameType | None = logging.currentframe()
        depth = 0
        while frame is not None and frame.f_code.co_filename in {logging.__file__, __file__}:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    """初始化日志。**幂等** —— 重复调用不会叠加 handler。

    级别取自 ``Settings.log_level``（配置唯一入口，见 M0-T04）。
    先 ``remove()`` 再 ``add()``，天然幂等；CLI / 测试 / 未来 Web 入口均可安全调用。
    """
    settings = get_settings()

    logger.remove()  # 清空既有 handler → 幂等
    logger.configure(patcher=_inject_trace_id)  # 逐条注入 trace_id
    logger.add(
        sys.stderr,
        format=LOG_FORMAT,
        level=settings.log_level,
        backtrace=False,  # 生产不开全栈回溯（避免噪音，M7 再按环境分层）
        diagnose=False,  # 关闭变量值诊断，防止密钥随异常泄露（安全红线）
    )

    # 收编 stdlib logging：第三方库 → InterceptHandler → loguru
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
