"""全链路 Trace 落盘与报告（M6-T03，方案 §5.2 支柱七）。

一个 trace 一个 JSON 文件（``{trace_store_dir}/{trace_id}.json``），保存
``AgentTrace`` 的完整快照（规划/Guard/风险/权限/工具/反思/指标），
并提供决策树式人类可读报告。

可靠性约定（与 SessionStore 一致）：
- 原子写：先写 ``.tmp`` 再 ``replace``
- 路径安全：trace_id 白名单校验，拒绝路径穿越
- 读取 fail-closed：损坏/版本不符/ID 不一致 → ``TraceStoreError``
- 列表 fail-soft：单个坏文件跳过并告警，不影响其他 trace
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from loguru import logger

from infrastructure.exceptions import TraceStoreError
from models.agent import AgentTrace
from safety.datasecurity import redact_text

_SCHEMA_VERSION = 1
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_RESULT_CHARS = 300
_PREVIEW_ROWS = 3
_MAX_RESPONSE_CHARS = 1000


@dataclass(frozen=True)
class TraceInfo:
    """Trace 列表条目（含归属字段，供 API 按用户/会话过滤）。"""

    trace_id: str
    user_input: str
    used_fallback: bool
    total_latency_ms: float
    saved_at: str
    user_id: str = ""
    session_id: str = ""


class TraceStore:
    """Trace 文件读写门面（一个实例对应一个存储目录）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """存储根目录（测试/CLI 展示用）。"""
        return self._root

    def path_for(self, trace_id: str | UUID) -> Path:
        """Trace 文件路径；非法 ID 直接拒绝（防路径穿越）。"""
        text = _trace_id_text(trace_id)
        return self._root / f"{text}.json"

    def save(self, trace: AgentTrace) -> Path:
        """原子写入一条 trace，返回文件路径。"""
        path = self.path_for(trace.trace_id)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "saved_at": _utc_now(),
            "trace": trace.model_dump(mode="json"),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            raise TraceStoreError(
                "Trace 写入失败", context={"trace_id": str(trace.trace_id)}
            ) from exc
        return path

    def load(self, trace_id: str | UUID) -> AgentTrace:
        """读取并校验一条 trace（fail-closed）。"""
        text = _trace_id_text(trace_id)
        path = self.path_for(text)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TraceStoreError("Trace 不存在", context={"trace_id": text}) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TraceStoreError("Trace 读取失败或文件损坏", context={"trace_id": text}) from exc

        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise TraceStoreError("Trace schema 版本不符", context={"trace_id": text})
        trace_payload = raw.get("trace")
        if not isinstance(trace_payload, dict):
            raise TraceStoreError("Trace 内容缺失", context={"trace_id": text})
        try:
            trace = AgentTrace.model_validate(trace_payload)
        except Exception as exc:  # Pydantic 细节不泄露到调用方
            raise TraceStoreError("Trace 内容不合法", context={"trace_id": text}) from exc
        if str(trace.trace_id) != text:
            raise TraceStoreError("Trace ID 与文件名不一致", context={"trace_id": text})
        return trace

    def list_traces(self) -> list[TraceInfo]:
        """列出目录内全部合法 trace（按保存时间倒序，坏文件跳过）。"""
        if not self._root.exists():
            return []
        infos: list[TraceInfo] = []
        for path in self._root.glob("*.json"):
            try:
                trace = self.load(path.stem)
                saved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
            except TraceStoreError as exc:
                logger.warning("跳过损坏的 trace 文件 | path={} error={}", path, exc)
                continue
            infos.append(
                TraceInfo(
                    trace_id=str(trace.trace_id),
                    user_input=redact_text(trace.user_input),
                    used_fallback=trace.used_fallback,
                    total_latency_ms=trace.total_latency_ms,
                    saved_at=saved_at,
                    user_id=trace.user_id,
                    session_id=trace.session_id,
                )
            )
        return sorted(infos, key=lambda info: info.saved_at, reverse=True)


def render_trace_report(trace: AgentTrace) -> str:
    """把 AgentTrace 渲染成决策树式报告。"""
    planning = trace.llm_planning or {}
    lines: list[str] = [
        f"Trace View (trace_id: {trace.trace_id})",
        f"├── 用户: {trace.user_id} ({trace.user_context.role}) / 会话: {trace.session_id or '-'}",
        f"├── 输入: {trace.user_input}",
        f"├── 降级: {'是' if trace.used_fallback else '否'}",
        "├── LLM Planning",
    ]
    if planning.get("fallback"):
        lines.append(
            "│   └── 规则引擎兜底: "
            f"intent={planning.get('intent') or '-'}, "
            f"reason={planning.get('reason') or '-'}"
        )
    else:
        lines.append(f"│   ├── 思考: {planning.get('reasoning') or '-'}")
        lines.append(f"│   └── Tool Calls: {_json(planning.get('tool_calls') or [])}")

    lines.append("├── Guard Layer")
    if trace.guard_checks:
        for index, item in enumerate(trace.guard_checks):
            mark = "✅" if item.get("passed") else "❌"
            reason = str(item.get("reason", ""))
            suffix = f"（{reason}）" if reason else ""
            branch = "└──" if index == len(trace.guard_checks) - 1 else "├──"
            lines.append(f"│   {branch} {item.get('guard', '-')}: {mark}{suffix}")
    else:
        lines.append("│   └── （无）")

    risk = trace.risk_assessment or {}
    lines.append(f"├── Risk: {_as_float(risk.get('score')):.1f} → {risk.get('level') or '-'}")
    permission = trace.permission_check or {}
    lines.append(
        "├── Permission: "
        f"allowed={permission.get('allowed', '-')} / "
        f"domain={permission.get('domain') or '-'} / "
        f"row_filter={permission.get('row_filter') or '-'}"
    )

    saga = trace.saga_transaction or {}
    if saga:
        lines.append(
            "├── Saga: "
            f"{saga.get('status', '-')}"
            f"（{len(saga.get('step_results', []))} 步 / "
            f"{len(saga.get('compensation_results', []))} 补偿）"
        )
    else:
        lines.append("├── Saga: -")

    lines.append("├── Tool Executions")
    if trace.tool_executions:
        for index, execution in enumerate(trace.tool_executions):
            branch = "└──" if index == len(trace.tool_executions) - 1 else "├──"
            if execution.error is not None:
                lines.append(f"│   {branch} {execution.tool_name} → 失败：{execution.error}")
                continue
            result = execution.result if isinstance(execution.result, dict) else {}
            rows = result.get("rows")
            rows = rows if isinstance(rows, list) else []
            lines.append(
                f"│   {branch} {execution.tool_name} "
                f"({execution.latency_ms:.0f}ms) → {len(rows)} 条"
            )
            for row in rows[:_PREVIEW_ROWS]:
                lines.append(f"│   │   └── {_json(row)}")
    else:
        lines.append("│   └── （无）")

    lines.append("├── Reflections")
    if trace.reflections:
        for index, reflection in enumerate(trace.reflections):
            branch = "└──" if index == len(trace.reflections) - 1 else "├──"
            lines.append(
                f"│   {branch} continue={reflection.get('continue_execution')} "
                f"next_input={reflection.get('next_input') or '-'}"
            )
    else:
        lines.append("│   └── （无）")

    lines.append(f"├── Final: {_preview(trace.final_response, _MAX_RESPONSE_CHARS)}")
    lines.append(
        "└── Metrics: "
        f"{trace.total_latency_ms:.0f}ms / "
        f"tokens={_json(trace.llm_token_usage)} / "
        f"api_calls={trace.kingdee_api_calls} / "
        f"hallucination={'是' if trace.hallucination_detected else '否'}"
    )
    return "\n".join(lines)


def _trace_id_text(trace_id: str | UUID) -> str:
    text = str(trace_id)
    if not _TRACE_ID_PATTERN.match(text):
        raise TraceStoreError("非法 trace_id", context={"trace_id": text})
    return text


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return _preview(text, _MAX_RESULT_CHARS)


def _preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
