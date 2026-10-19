"""Tool 选对率评测集加载（M8-T01：迁移到版本化 JSON）。

数据文件：``evals/datasets/v1/tool_selection.json``
- 加载期校验：版本、必填字段、ID 唯一、期望工具必须已注册
- 导出 ``QUESTIONS`` 与 ``EvalQuestion`` 供旧脚本/测试兼容
"""

from pathlib import Path

from agent.tools import build_default_registry
from governance.evaluation.dataset import EvalDataset, EvalQuestion, load_dataset

_DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "v1" / "tool_selection.json"
_REGISTERED_TOOLS = frozenset(spec.name for spec in build_default_registry().all_specs())

_DATASET: EvalDataset = load_dataset(_DATASET_PATH, known_tools=_REGISTERED_TOOLS)

#: 评测集版本（报告与门禁记录用）
DATASET_VERSION: str = _DATASET.version

#: 兼容旧接口：固定问题集
QUESTIONS: tuple[EvalQuestion, ...] = _DATASET.questions

__all__ = ["DATASET_VERSION", "QUESTIONS", "EvalDataset", "EvalQuestion"]
