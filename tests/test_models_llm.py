"""LLM 边界模型单测（M2-T01）。"""

import pytest
from pydantic import ValidationError

from models.llm import (
    ExecutionSummary,
    LLMContext,
    LLMMessage,
    MemorySnapshot,
    PlanResult,
    ReflectionResult,
    TokenUsage,
    ToolCallDecision,
    ToolSpec,
)


def test_llm_message_role_is_closed_set() -> None:
    """角色是闭集：非法 role 直接拒绝（fail-closed）。"""
    with pytest.raises(ValidationError):
        LLMMessage(role="invalid_role", content="x")


def test_llm_message_rejects_unknown_field() -> None:
    """extra="forbid"：未知字段拒绝（全局模型规范）。

    Why 走 model_validate(dict)：负面输入在静态层就不该通过 mypy，
    字典构造绕过静态检查、保留运行时 Pydantic 校验路径。
    """
    with pytest.raises(ValidationError):
        LLMMessage.model_validate({"role": "user", "content": "x", "unexpected_field": 1})


def test_llm_context_defaults_empty() -> None:
    """消息与锚点默认空列表（纯聊天场景零配置构造）。"""
    context = LLMContext()
    assert context.messages == []
    assert context.anchors == []


def test_tool_spec_requires_name_and_description() -> None:
    """Schema 即文档：name/description 必填且非空。"""
    with pytest.raises(ValidationError):
        ToolSpec(name="", description="x")
    with pytest.raises(ValidationError):
        ToolSpec(name="t", description="")


def test_plan_result_defaults_to_no_tool_calls() -> None:
    """纯聊天无需工具：tool_calls 默认空。"""
    assert PlanResult().tool_calls == []


def test_reflection_result_requires_continue_execution() -> None:
    """续跑决策是显式布尔：缺省必须拒绝（不许隐式 False）。"""
    with pytest.raises(ValidationError):
        ReflectionResult.model_validate({})


def test_execution_summary_defaults() -> None:
    """执行摘要默认：无记录、结果已足够（needs_more_steps=False）。"""
    summary = ExecutionSummary()
    assert summary.tool_executions == []
    assert summary.needs_more_steps is False


def test_memory_snapshot_three_layers() -> None:
    """三层记忆快照字段齐备（方案 §5.3 支柱九的 LLM 视角）。"""
    snapshot = MemorySnapshot(
        layer1_recent=["用户问：最近订单？"],
        session_summary="会话主题：订单查询",
        user_profile="运营人员，常查销售订单",
    )
    assert snapshot.layer1_recent == ["用户问：最近订单？"]
    assert snapshot.session_summary == "会话主题：订单查询"
    assert snapshot.user_profile == "运营人员，常查销售订单"


def test_token_usage_total_sums_parts() -> None:
    """total_tokens = prompt + completion（trace 用量来源）。"""
    usage = TokenUsage(prompt_tokens=100, completion_tokens=20)
    assert usage.total_tokens == 120


def test_tool_call_decision_arguments_default_empty() -> None:
    """无参工具：arguments 默认空 dict，tool_name 必填。"""
    decision = ToolCallDecision(tool_name="no_param_tool")
    assert decision.arguments == {}
    assert decision.rationale == ""
