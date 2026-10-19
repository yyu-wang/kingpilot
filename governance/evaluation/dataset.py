"""评测集加载与版本校验（M8-T01）。

评测集以 JSON 版本化存储：路径含 ``vN``、文件内再带 ``version`` 字段。
加载期做三类 fail-fast 校验：
1. 结构完整（version / cases / 必填字段）
2. 用例 ID 唯一
3. 期望工具存在于注册表（工具改名漏改时当场暴露）

工具名不做硬编码校验：调用方传入当前注册表工具集合，保持本模块与
agent 层解耦。
"""

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infrastructure.exceptions import EvaluationError


@dataclass(frozen=True)
class EvalQuestion:
    """评测集单题：问题 + 期望工具（None = 期望纯聊天、不调任何工具）。"""

    question_id: str
    question: str
    expected_tool: str | None
    category: str


@dataclass(frozen=True)
class EvalDataset:
    """一份版本化评测集。"""

    version: str
    questions: tuple[EvalQuestion, ...]


def load_dataset(
    path: Path,
    *,
    known_tools: Collection[str] | None = None,
) -> EvalDataset:
    """读取并校验评测集；坏数据抛 ``EvaluationError``（fail-closed）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvaluationError(f"评测集读取失败: {path}", context={"path": str(path)}) from exc
    except ValueError as exc:
        raise EvaluationError(f"评测集不是合法 JSON: {path}", context={"path": str(path)}) from exc

    if not isinstance(payload, dict):
        raise EvaluationError("评测集根节点必须是对象")

    version = str(payload.get("version") or "").strip()
    if not version:
        raise EvaluationError("评测集缺少 version 字段")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationError("评测集 cases 必须是非空数组")

    questions: list[EvalQuestion] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise EvaluationError(f"cases[{index}] 必须是对象")
        question = _parse_question(raw, index)
        if question.question_id in seen_ids:
            raise EvaluationError(f"用例 ID 重复: {question.question_id}")
        seen_ids.add(question.question_id)
        if (
            known_tools is not None
            and question.expected_tool is not None
            and question.expected_tool not in known_tools
        ):
            raise EvaluationError(
                f"用例 {question.question_id} 期望工具未注册: {question.expected_tool}",
                context={"known_tools": ", ".join(sorted(known_tools))},
            )
        questions.append(question)

    return EvalDataset(version=version, questions=tuple(questions))


def _parse_question(raw: dict[str, Any], index: int) -> EvalQuestion:
    """单题字段解析与必填校验。"""
    question_id = _required_text(raw, "question_id", index)
    question = _required_text(raw, "question", index)
    category = _required_text(raw, "category", index)

    expected_tool = raw.get("expected_tool")
    if expected_tool is not None and not isinstance(expected_tool, str):
        raise EvaluationError(f"cases[{index}].expected_tool 必须是字符串或 null")
    if isinstance(expected_tool, str) and not expected_tool.strip():
        raise EvaluationError(f"cases[{index}].expected_tool 不能为空串")

    return EvalQuestion(
        question_id=question_id,
        question=question,
        expected_tool=expected_tool,
        category=category,
    )


def _required_text(raw: dict[str, Any], field: str, index: int) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"cases[{index}].{field} 必须是非空字符串")
    return value.strip()
