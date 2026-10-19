"""审计事件统一出口（M6-T04）。

- ``JsonlAuditSink``：append-only JSONL（一行一事件），父目录自动创建
- ``AuditLogger``：写 sink + 通知订阅者（T05 告警引擎）
- 审计写失败只写 CRITICAL，不阻塞业务；订阅者异常不扩散

M4 的 Guard 拦截、M5 的账本写入在注入本出口后统一进审计流；
未注入时各组件保留原结构化日志（兼容既有测试）。
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from loguru import logger
from pydantic import ValidationError

from models.audit import AuditEvent

AuditObserver = Callable[[AuditEvent], None]


class AuditSink(Protocol):
    """审计事件落盘/转发接口。"""

    def write(self, event: AuditEvent) -> None:
        """写入一条事件；失败可抛异常，由 AuditLogger 统一告警。"""
        ...


class JsonlAuditSink:
    """append-only JSONL 文件出口（本地合规留存）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def write(self, event: AuditEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class JsonlAuditReader:
    """审计 JSONL 读取器（M9-T06）：fail-soft，坏行跳过。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read_all(self) -> list[AuditEvent]:
        """读取全部合法事件；文件不存在/不可读返回空列表。"""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("审计文件读取失败: path={} error={}", self._path, exc)
            return []

        events: list[AuditEvent] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                events.append(AuditEvent.model_validate(json.loads(line)))
            except (ValueError, ValidationError) as exc:
                logger.warning("跳过损坏的审计行: path={} error={}", self._path, exc)
        return events


class AuditLogger:
    """审计门面：统一写出口 + 订阅通知。"""

    def __init__(self, sink: AuditSink) -> None:
        self._sink = sink
        self._observers: list[AuditObserver] = []

    def subscribe(self, observer: AuditObserver) -> None:
        """订阅审计事件（T05 AlertEngine 使用）。"""
        self._observers.append(observer)

    def emit(self, event: AuditEvent) -> None:
        """写审计出口并通知订阅者；任何失败都不向业务抛异常。"""
        try:
            self._sink.write(event)
        except Exception as exc:  # 审计丢失必须大声告警，但不阻塞业务
            logger.critical("审计事件写入失败 | event_type={} error={}", event.event_type, exc)

        for observer in list(self._observers):
            try:
                observer(event)
            except Exception as exc:
                logger.warning(
                    "审计订阅者异常 | event_type={} error={}",
                    event.event_type,
                    exc,
                )
