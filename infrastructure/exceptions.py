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
