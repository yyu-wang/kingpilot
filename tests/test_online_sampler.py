"""M8-T02：在线抽样单测。

覆盖开关/比例/异常强制采样、每日上限、落盘脱敏、
样本回流评测集草稿，以及 CLI 循环的抽样接线。
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from agent.cli import run_chat_loop
from agent.core import AgentCore
from agent.memory import MemoryStore
from agent.session_store import SessionStore
from governance.online_sampler import OnlineSampler, iter_samples
from models.agent import AgentResponse, AgentTrace
from models.tool import ToolExecution
from models.user import UserContext
from safety.dlp import DlpFinding, DlpScanner, DlpScanResult

_NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
_RAW_PHONE = "13812345678"
_RAW_ID_CARD = "110101199001011234"


def _trace(
    user_input: str = "查订单 SO000008",
    *,
    used_fallback: bool = False,
    tool_error: str = "",
) -> AgentTrace:
    trace = AgentTrace(
        user_input=user_input,
        user_context=UserContext(user_id="u001", role="viewer"),
        user_id="u001",
    )
    trace.used_fallback = used_fallback
    if tool_error:
        trace.tool_executions.append(ToolExecution(tool_name="t", parameters={}, error=tool_error))
    return trace


def _response(
    content: str = "查询完成",
    *,
    blocked: bool = False,
    trace: AgentTrace | None = None,
) -> AgentResponse:
    resolved = trace or _trace()
    resolved.final_response = content
    return AgentResponse(
        content=content, trace_id=resolved.trace_id, blocked=blocked, trace=resolved
    )


def _sampler(
    tmp_path: Path, *, rate: float = 1.0, max_per_day: int = 500, rng: float = 0.0
) -> OnlineSampler:
    return OnlineSampler(
        tmp_path / "samples",
        sample_rate=rate,
        max_per_day=max_per_day,
        clock=lambda: _NOW,
        rng=lambda: rng,
    )


# ── 开关与比例 ──


def test_disabled_sampler_never_writes(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path, rate=0.0)
    assert sampler.enabled is False

    assert sampler.maybe_sample(_response(blocked=True)) is None
    assert not (tmp_path / "samples").exists()


def test_rate_one_writes_one_jsonl_line(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path, rate=1.0)

    path = sampler.maybe_sample(_response(), session_id="s1")

    assert path == tmp_path / "samples" / "2026-10-19.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    sample = json.loads(lines[0])
    assert sample["schema_version"] == 1
    assert sample["session_id"] == "s1"
    assert sample["forced"] is False
    assert sample["trace"]["user_input"] == "查订单 SO000008"


def test_rate_controls_normal_sampling(tmp_path: Path) -> None:
    assert _sampler(tmp_path, rate=0.1, rng=0.5).maybe_sample(_response()) is None
    path = _sampler(tmp_path, rate=0.1, rng=0.05).maybe_sample(_response())
    assert path is not None


@pytest.mark.parametrize(
    "response",
    [
        _response(blocked=True),
        _response(trace=_trace(used_fallback=True)),
        _response(trace=_trace(tool_error="金蝶超时")),
    ],
)
def test_abnormal_response_is_always_sampled(tmp_path: Path, response: AgentResponse) -> None:
    """异常样本不受采样率影响（rng 取 0.99 仍应写入）。"""
    sampler = _sampler(tmp_path, rate=0.01, rng=0.99)

    path = sampler.maybe_sample(response)

    assert path is not None
    sample = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert sample["forced"] is True


# ── 脱敏 ──


def test_sample_is_redacted(tmp_path: Path) -> None:
    trace = _trace(f"客户手机 {_RAW_PHONE}")
    response = _response(f"身份证 {_RAW_ID_CARD}", trace=trace)
    sampler = _sampler(tmp_path)

    path = sampler.maybe_sample(response)

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert _RAW_PHONE not in text
    assert _RAW_ID_CARD not in text
    assert "138****5678" in text


# ── 每日上限 ──


def test_daily_cap_stops_writing(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path, max_per_day=2)

    assert sampler.maybe_sample(_response()) is not None
    assert sampler.maybe_sample(_response()) is not None
    assert sampler.maybe_sample(_response()) is None

    path = tmp_path / "samples" / "2026-10-19.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_invalid_constructor_args(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="采样率"):
        _sampler(tmp_path, rate=1.5)
    with pytest.raises(ValueError, match="每日样本上限"):
        _sampler(tmp_path, max_per_day=0)


def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    """目录位置被文件占用时只告警，不影响主流程。"""
    blocker = tmp_path / "samples"
    blocker.write_text("not a dir", encoding="utf-8")
    sampler = _sampler(tmp_path)

    assert sampler.maybe_sample(_response()) is None


class _AlwaysBlockedScanner:
    def scan(self, text: str) -> DlpScanResult:
        return DlpScanResult(findings=(DlpFinding(category="phone", count=1),))


def test_dlp_hit_rejects_sample(tmp_path: Path) -> None:
    """脱敏后仍命中 DLP 时拒写（fail-closed）。"""
    sampler = OnlineSampler(
        tmp_path / "samples",
        sample_rate=1.0,
        clock=lambda: _NOW,
        rng=lambda: 0.0,
        scanner=cast(DlpScanner, _AlwaysBlockedScanner()),
    )

    assert sampler.maybe_sample(_response()) is None
    assert not (tmp_path / "samples").exists()


# ── 回流评测集草稿 ──


def test_export_eval_case_candidates(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path)
    sampler.maybe_sample(_response("第一轮", trace=_trace("查订单 SO000008")))
    sampler.maybe_sample(_response("第二轮", trace=_trace("查客户 CUS001")))
    sampler.maybe_sample(_response("重复", trace=_trace("查订单 SO000008")))

    path = tmp_path / "samples" / "2026-10-19.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("not-json\n")
        handle.write('{"trace": "not-a-dict"}\n')

    candidates = sampler.to_eval_case_candidates(path)

    assert [case["question"] for case in candidates] == ["查订单 SO000008", "查客户 CUS001"]
    assert candidates[0]["question_id"] == "ONLINE-0001"
    assert candidates[0]["expected_tool"] is None
    assert candidates[0]["category"] == "在线样本（待标注）"


def test_iter_samples_skips_bad_lines(tmp_path: Path) -> None:
    sampler = _sampler(tmp_path)
    path = sampler.maybe_sample(_response())
    assert path is not None
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\nnot-json\n")

    samples = list(iter_samples(path))

    assert len(samples) == 1
    assert samples[0]["trace"]["user_input"] == "查订单 SO000008"
    assert list(iter_samples(tmp_path / "missing.jsonl")) == []


def test_export_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _sampler(tmp_path).to_eval_case_candidates(tmp_path / "nope.jsonl") == []


# ── CLI 接线 ──


class _RecordingSampler:
    def __init__(self) -> None:
        self.calls: list[tuple[AgentResponse, str]] = []

    def maybe_sample(self, response: AgentResponse, *, session_id: str = "") -> None:
        self.calls.append((response, session_id))


class _EchoCore:
    def __init__(self, response: AgentResponse) -> None:
        self._response = response
        self.inputs: list[str] = []

    async def run(self, user_input: str) -> AgentResponse:
        self.inputs.append(user_input)
        return self._response


async def test_chat_loop_calls_sampler_per_turn(tmp_path: Path) -> None:
    response = _response()
    core = _EchoCore(response)
    sampler = _RecordingSampler()
    inputs = iter(["第一轮", "第二轮", "/exit"])

    await run_chat_loop(
        cast(AgentCore, core),
        MemoryStore(session_id="s1"),
        SessionStore(tmp_path / "sessions"),
        read_input=lambda _prompt: next(inputs),
        write_output=lambda _line: None,
        sampler=cast(OnlineSampler, sampler),
    )

    assert len(sampler.calls) == 2
    assert sampler.calls[0][1] == "s1"
    assert core.inputs == ["第一轮", "第二轮"]
