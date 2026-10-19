"""统一错误契约（M9-T01）。

所有非 2xx 响应统一为 ``{"error": {"code", "message"}}``：
- 不回传 ``str(exc)`` / ``context`` / 堆栈 / 上游响应体
- 参数校验错误由自定义处理器接管，避免 FastAPI 默认回显请求原值
"""

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.schemas import ErrorBody, ErrorEnvelope
from infrastructure.exceptions import ApprovalError, BudgetExceededError, KingPilotError

_APPROVAL_HTTP: dict[str, int] = {
    "approval_not_found": 404,
    "approval_forbidden": 403,
    "approval_conflict": 409,
    "approval_expired": 409,
    "approval_store_failed": 500,
}


class ApiError(Exception):
    """可安全返回给客户端的业务错误。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def _envelope(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorBody(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump(), headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """注册统一异常处理器（顺序不影响 Starlette 的精确类型匹配）。"""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.status_code, exc.code, exc.message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 不回显 pydantic 的 input/ctx，避免把用户原值写进响应
        return _envelope(422, "invalid_request", "请求参数不合法")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _envelope(404, "not_found", "资源不存在")
        return _envelope(exc.status_code, "http_error", "请求失败")

    @app.exception_handler(BudgetExceededError)
    async def _budget_exhausted(request: Request, exc: BudgetExceededError) -> JSONResponse:
        return _envelope(503, "budget_exhausted", "服务预算已耗尽，请稍后重试")

    @app.exception_handler(ApprovalError)
    async def _approval_error(request: Request, exc: ApprovalError) -> JSONResponse:
        status_code = _APPROVAL_HTTP.get(exc.code, 500)
        return _envelope(status_code, exc.code, str(exc))

    @app.exception_handler(KingPilotError)
    async def _agent_error(request: Request, exc: KingPilotError) -> JSONResponse:
        # 异常消息按项目红线不含凭证；context 仍不返回给客户端
        logger.warning("API 业务异常: {}", exc)
        return _envelope(502, "agent_error", "Agent 处理失败")

    @app.exception_handler(Exception)
    async def _internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("API 未预期异常")
        return _envelope(500, "internal_error", "服务内部错误")
