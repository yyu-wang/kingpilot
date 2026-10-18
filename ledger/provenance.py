"""证据链构建与报告（M5-T05，方案 §5.5 支柱十七）。

职责：
1. 从 ``AgentTrace`` + 本次工具调用 + 执行器提供的 API 请求/响应，
   构建 ``ProvenanceChain``（谁说的 → LLM 怎么理解 → 过了什么检查 →
   谁确认 → 金蝶返回什么）
2. 按 trace_id / business_key 从账本读取记录并输出人类可读报告

设计要点：
- 纯函数/只读服务，不写账本（落账归 T03/T06）
- 缺失字段（无安全层、无确认记录）降级为空值，不抛错
- API 请求/响应由执行器传入，调用方负责脱敏（本项目当前无真实写接口）
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ledger.write_ledger import WriteLedger
from models.agent import AgentTrace
from models.ledger import ProvenanceChain, WriteRecord
from models.llm import ToolCallDecision


def build_provenance(
    trace: AgentTrace,
    tool_call: ToolCallDecision,
    *,
    tool_call_params: Mapping[str, Any] | None = None,
    api_request: Mapping[str, Any] | None = None,
    api_response: Mapping[str, Any] | None = None,
    confirmation_record: Mapping[str, Any] | None = None,
) -> ProvenanceChain:
    """构建一条写操作的完整证据链。

    Args:
        trace: 主循环全链路追踪。
        tool_call: LLM 的原始调用（arguments 用于兜底参数）。
        tool_call_params: 最终执行参数（如 Guard 3 换码后的参数）。
        api_request / api_response: 实际写接口报文（调用方脱敏后传入）。
        confirmation_record: 确认记录；缺省从 ``trace.risk_assessment["hitl"]`` 取。
    """
    risk = trace.risk_assessment or {}
    permission = trace.permission_check or {}
    confirmation = (
        dict(confirmation_record)
        if confirmation_record is not None
        else _as_mapping(risk.get("hitl"))
    )
    return ProvenanceChain(
        user_original_input=trace.user_input,
        user_id=trace.user_id,
        session_id=trace.session_id,
        llm_reasoning=str(trace.llm_planning.get("reasoning", "")),
        tool_call_params=dict(tool_call_params or tool_call.arguments),
        guard_check_results=[dict(item) for item in trace.guard_checks],
        risk_score=_as_float(risk.get("score")),
        risk_level=str(risk.get("level", "")),
        confirmation_record=confirmation,
        approval_chain=_as_str_list(permission.get("required_approvers")),
        api_request=dict(api_request or {}),
        api_response=dict(api_response or {}),
    )


def render_provenance_report(record: WriteRecord) -> str:
    """把一条账本记录渲染成人类可读的决策链报告。"""
    chain = record.provenance
    lines: list[str] = [
        "【写操作溯源】",
        f"单据：{record.form_id} / {record.business_key}（{record.action}）",
        f"时间：{record.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
        f"状态：{record.status}",
        f"用户：{chain.user_id}（会话 {chain.session_id or '-'}）",
        f"用户原话：{chain.user_original_input or '-'}",
        f"LLM 理解：{chain.llm_reasoning or '-'}",
        f"工具参数：{_json(chain.tool_call_params)}",
        "Guard 检查：",
    ]
    if chain.guard_check_results:
        for item in chain.guard_check_results:
            name = str(item.get("guard", "-"))
            passed = "✅ 通过" if item.get("passed") else "❌ 拦截"
            reason = str(item.get("reason", ""))
            lines.append(f"- {name}: {passed}" + (f"（{reason}）" if reason else ""))
    else:
        lines.append("-（无记录）")

    lines.extend(
        [
            f"风险评分：{chain.risk_score:.1f}（{chain.risk_level or '-'}）",
            f"确认记录：{_json(chain.confirmation_record) if chain.confirmation_record else '-'}",
            f"审批链：{'、'.join(chain.approval_chain) if chain.approval_chain else '-'}",
            f"API 请求：{_json(chain.api_request)}",
            f"API 响应：{_json(chain.api_response)}",
            "字段变更：",
        ]
    )
    if record.field_diff:
        for diff in record.field_diff:
            lines.append(f"- {diff.field_name}: {diff.old_value!r} → {diff.new_value!r}")
    else:
        lines.append("-（无变更）")

    lines.append(f"证据 hash：prev={record.prev_record_hash} record={record.record_hash}")
    return "\n".join(lines)


def render_trace_report(records: Sequence[WriteRecord]) -> str:
    """渲染一次请求（trace）下的全部写记录。"""
    if not records:
        return "未找到对应的账本记录。"
    return "\n\n---\n\n".join(render_provenance_report(record) for record in records)


class ProvenanceReporter:
    """按 trace_id / business_key 输出决策链报告。"""

    def __init__(self, ledger: WriteLedger) -> None:
        self._ledger = ledger

    def report_by_trace_id(self, trace_id: str) -> str:
        """一次请求的全部写操作证据链。"""
        return render_trace_report(self._ledger.find_by_trace_id(trace_id))

    def report_by_business_key(self, business_key: str) -> str:
        """一张单据的全部变更证据链（审计场景 6）。"""
        return render_trace_report(self._ledger.find_by_business_key(business_key))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _as_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return dict(value)


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


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
