"""Guard 框架（M4-T01）：五层护栏的统一输入/输出 + 顺序执行管道。

设计依据（开发计划 M4 模块规范）：
1. Guard 是**纯函数**（无状态、不写外部数据；只读查询允许，故统一 async）
2. **Fail-closed**：空管道构造即拒绝；Guard 抛异常按拦截处理，不向上传播
3. 规则与逻辑分离：本模块只定框架，具体规则归 T02~T06 / config/
4. 每次拦截产生一条结构化审计日志（字段对齐 M6 审计事件）
5. 短路语义：首个 blocked 之后的 Guard 不再执行，节省调用并保证结论确定

与主循环的关系：Phase 2（``AgentCore._phase2_guard``）在 M4-T10 接入；
本模块不依赖 agent/kingdee，避免循环导入。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from loguru import logger

from models.user import UserContext

# 审计事件名（M6 落地前先写结构化日志，字段与审计事件对齐）
_EVENT_GUARD_BLOCKED = "guard.blocked"


@dataclass(frozen=True)
class GuardContext:
    """一次工具调用进入护栏管道时的只读上下文。

    ``arguments`` 用 Mapping 声明只读语义：Guard 只能看参数，不能改参数——
    任何改写（如行级条件注入）都属于主循环/执行层的显式动作，不在 Guard 内偷做。
    """

    tool_name: str
    arguments: Mapping[str, Any]
    user_context: UserContext
    trace_id: str = ""


@dataclass(frozen=True)
class GuardResult:
    """单个 Guard 的判定结果。

    Why block 强制要求 reason：拦截必须有可解释原因，空原因等于「静默拒绝」，
    线上无法排查也无法向用户解释——这是 fail-closed 的可用性底线。
    """

    passed: bool
    reason: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, *, detail: Mapping[str, Any] | None = None) -> "GuardResult":
        """放行（detail 可携带校验摘要，供审计留痕）。"""
        return cls(passed=True, detail=dict(detail or {}))

    @classmethod
    def block(cls, reason: str, *, detail: Mapping[str, Any] | None = None) -> "GuardResult":
        """拦截；``reason`` 为空白字符串时直接报错（装配期错误尽早暴露）。

        Raises:
            ValueError: 拦截原因为空。
        """
        if not reason.strip():
            raise ValueError("拦截结果必须携带原因")
        return cls(passed=False, reason=reason, detail=dict(detail or {}))


class Guard(Protocol):
    """护栏协议：具名 + 异步校验。

    async 是统一接口而非当前需要：T04 实体存在性预检要查金蝶，
    提前统一可避免后续改签名波及管道与所有实现。
    """

    name: str

    async def check(self, context: GuardContext) -> GuardResult:
        """校验一次工具调用；实现必须无状态。"""
        ...


@dataclass(frozen=True)
class GuardCheckRecord:
    """单个 Guard 的执行记录（写进 ``AgentTrace.guard_checks`` 的最小单元）。"""

    guard_name: str
    passed: bool
    reason: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为可直接 JSON 序列化的审计字典。"""
        return {
            "guard": self.guard_name,
            "passed": self.passed,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class GuardPipelineResult:
    """管道整体结论 + 已执行 Guard 的逐条记录。

    ``effective_arguments`` 是应用 Guard 产出的 ``parameter_updates`` 之后的
    最终参数（如 Guard 3 把实体名称换成内码 Id）——T10 执行时必须用它，
    而不是 LLM 的原始参数。
    """

    blocked: bool
    reason: str = ""
    blocked_by: str = ""
    records: tuple[GuardCheckRecord, ...] = ()
    effective_arguments: Mapping[str, Any] = field(default_factory=dict)

    def to_records(self) -> list[dict[str, Any]]:
        """逐条记录转字典（Phase 2 直接 extend 进 trace.guard_checks）。"""
        return [record.to_dict() for record in self.records]


class GuardPipeline:
    """顺序执行、首个拦截即短路的护栏管道。"""

    def __init__(self, guards: Sequence[Guard]) -> None:
        """装配管道。

        Raises:
            ValueError: Guard 列表为空——无校验即放行违反 fail-closed，
                该错误必须在装配期暴露，而不是运行期静默放行。
        """
        if not guards:
            raise ValueError("Guard 管道不能为空（fail-closed：无校验不放行）")
        self._guards = tuple(guards)

    @property
    def guard_names(self) -> tuple[str, ...]:
        """按执行顺序返回 Guard 名（装配自检/日志用）。"""
        return tuple(guard.name for guard in self._guards)

    async def check(self, context: GuardContext) -> GuardPipelineResult:
        """逐个执行 Guard；首个拦截立即返回，后续 Guard 不再调用。

        Guard 抛出的任何异常都转成拦截结论（fail-closed），并记录异常类型；
        不向上抛是为了避免单个 Guard 的缺陷打断整条主循环。

        某个 Guard 放行且 detail 含 ``parameter_updates`` 时，更新会合并进
        后续 Guard 的上下文（如 Guard 3 换码后，Guard 5 才能看到内码 Id）。
        """
        records: list[GuardCheckRecord] = []
        current_arguments: dict[str, Any] = dict(context.arguments)
        current_context = context
        for guard in self._guards:
            try:
                result = await guard.check(current_context)
            except Exception as exc:  # fail-closed：任何异常都视为校验失败
                logger.warning(
                    "护栏执行异常，按拦截处理 | guard={} tool={} user={} trace_id={} error={}: {}",
                    guard.name,
                    current_context.tool_name,
                    current_context.user_context.user_id,
                    current_context.trace_id,
                    type(exc).__name__,
                    exc,
                )
                result = GuardResult.block(
                    f"护栏 {guard.name} 执行异常，按拦截处理：{type(exc).__name__}: {exc}",
                    detail={"exception": type(exc).__name__},
                )

            if not isinstance(result, GuardResult):
                # 防御实现方违约（如返回 None）：无法判定 = 拒绝
                result = GuardResult.block(
                    f"护栏 {guard.name} 返回非法结果，按拦截处理",
                    detail={"returned_type": type(result).__name__},
                )

            records.append(
                GuardCheckRecord(
                    guard_name=guard.name,
                    passed=result.passed,
                    reason=result.reason,
                    detail=result.detail,
                )
            )
            if not result.passed:
                logger.warning(
                    "安全护栏拦截 | event={} guard={} tool={} user={} trace_id={} reason={}",
                    _EVENT_GUARD_BLOCKED,
                    guard.name,
                    current_context.tool_name,
                    current_context.user_context.user_id,
                    current_context.trace_id,
                    result.reason,
                )
                return GuardPipelineResult(
                    blocked=True,
                    reason=result.reason,
                    blocked_by=guard.name,
                    records=tuple(records),
                    effective_arguments=current_arguments,
                )

            updates = result.detail.get("parameter_updates")
            if isinstance(updates, Mapping) and updates:
                current_arguments = {
                    **current_arguments,
                    **{str(key): value for key, value in updates.items()},
                }
                current_context = replace(current_context, arguments=current_arguments)

        return GuardPipelineResult(
            blocked=False,
            records=tuple(records),
            effective_arguments=current_arguments,
        )
