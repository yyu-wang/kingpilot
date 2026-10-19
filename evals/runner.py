"""兼容层（M8-T01）：判定/统计逻辑已迁移到 ``governance.evaluation``。

保留本模块的导出名，避免既有脚本与测试大改；新代码请直接从
``governance.evaluation.offline_eval`` 导入。
"""

from governance.evaluation.dataset import EvalDataset, EvalQuestion
from governance.evaluation.offline_eval import (
    EvaluationReport,
    QuestionVerdict,
    check_params,
    judge,
    report_to_dict,
    run_evaluation,
)

__all__ = [
    "EvalDataset",
    "EvalQuestion",
    "EvaluationReport",
    "QuestionVerdict",
    "check_params",
    "judge",
    "report_to_dict",
    "run_evaluation",
]
