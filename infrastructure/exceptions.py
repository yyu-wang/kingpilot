"""统一异常层次（M1-T03，按规范 §3.4 建立根 + 金蝶分支）。

只建当前模块需要的分支；Safety/LLM/Saga/Ledger 分支随各自模块落地时追加，
不超前建模（YAGNI）。

红线（规范 3.5/3.6）：异常消息永不含凭证、密钥、签名；必须携带可定位上下文
（操作名 / formId），trace_id 由日志层统一注入、不进异常消息。
"""

from typing import Any


class KingPilotError(Exception):
    """项目异常根。``context`` 供日志层结构化输出，不进 ``str(exc)`` 明文。"""

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.context: dict[str, Any] = context or {}


class KingdeeError(KingPilotError):
    """金蝶适配层异常基类。"""


class KingdeeAuthError(KingdeeError):
    """认证失败：签名登录被拒、会话刷新失败、凭证缺失。"""


class KingdeeRateLimitError(KingdeeError):
    """触发金蝶侧限流（client 退避后仍失败时抛出，T04 使用）。"""


class KingdeeAPIError(KingdeeError):
    """金蝶业务错误：HTTP 200 但 IsSuccess=false，或响应形状破坏协议。"""


class LLMError(KingPilotError):
    """LLM 层异常基类（方案 §3.4 层次表）。"""


class LLMUnavailableError(LLMError):
    """LLM 不可用：超时、网络失败、服务端 5xx。

    主循环降级的唯一触发点（方案 §2.3 Phase 1 只 catch 本异常）——
    其它 LLM 异常不触发降级，避免把确定性错误（如响应不合法）误判为
    「服务暂时不可用」而进入规则引擎兜底。
    """


class LLMRateLimitError(LLMError):
    """触发 LLM 供应商限流（429）——模型级 failover 链的触发条件之一（方案 §支柱十二）。"""


class LLMResponseError(LLMError):
    """LLM 响应不合法：JSON 解析失败、缺必需字段、tool call 参数不符 Schema。

    确定性错误，重试同样请求大概率复现——不触发降级，由调用方决定
    纠错（如重发带修正提示的请求）或直接失败。
    """


class ToolError(KingPilotError):
    """工具层异常（M2-T03）：参数校验失败、组装被拒、未知工具、重复注册。

    Why 挂在根下而非 Kingdee/LLM 分支：工具是编排层概念，一个工具的失败
    可能源自任一下游（参数错/金蝶错），调用方按根类型接住后看 context 定位。
    """


class SessionStoreError(KingPilotError):
    """会话持久化异常（M3-T07）：文件损坏 / schema 版本不符 / 路径非法。

    fail-closed：宁可显式报错让用户介入，也不静默丢弃历史或覆盖存档。
    """


class IdempotencyError(KingPilotError):
    """幂等层异常（M5-T01）：同一 Key 正在处理、存储状态非法等。

    fail-closed：幂等状态不确定时宁可拒绝也不重复执行写操作。
    """


class SnapshotError(KingPilotError):
    """写前快照不可用（M5-T06）：编辑/反审核/删除必须查到原状态才能写。

    fail-closed：查不到原状态 = 盲改，直接中止写入并由 Saga 补偿。
    """


class TraceStoreError(KingPilotError):
    """Trace 落盘异常（M6-T03）：文件损坏 / schema 版本不符 / ID 不一致。

    读取 fail-closed；写入失败由主循环降级为告警，不阻塞用户响应。
    """


class DeadLetterError(KingPilotError):
    """死信队列异常（M6-T06）：记录不存在/损坏/路径非法、重复重放。

    fail-closed：死信状态不确定时宁可报错，也不静默丢掉待人工处理的事务。
    """


class BudgetExceededError(KingPilotError):
    """LLM 预算耗尽（M7-T04）：经济档仍超预算时拒绝继续调用。

    fail-closed：宁可中止本次请求，也不在无预算保障下继续产生费用。
    """


class EvaluationError(KingPilotError):
    """评测集/报告异常（M8-T01）：文件缺失、JSON 非法、字段缺失或工具名漂移。

    fail-closed：评测集是发布门禁的输入，坏数据必须当场拒绝而不是跑出假指标。
    """


class ReleaseGateError(KingPilotError):
    """发布门禁异常（M8-T03）：报告/基线缺失、JSON 非法、指标字段不合法。

    fail-closed：无法证明「没有退化」时一律不放行。
    """


class PromptRegistryError(KingPilotError):
    """Prompt 版本管理异常（M8-T04）：配置缺失、版本文件不存在、灰度比例非法。

    fail-closed：Prompt 配置错误直接影响所有 LLM 调用，必须在启动期暴露。
    """


class ApiKeyConfigError(KingPilotError):
    """API Key 配置非法（M9-T01）：JSON 损坏、哈希格式错、身份字段非法。

    fail-closed：配置错误必须在启动期暴露，不能等到请求时才 401。
    """
