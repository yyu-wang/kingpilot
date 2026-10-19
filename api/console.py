"""静态控制台托管与安全响应头（M9-T03）。

- `/console/` 由 Starlette StaticFiles 同源托管（html=True 支持 index.html）
- `/` 重定向到控制台
- CSP 只允许同源脚本/样式/连接，杜绝 CDN 与内联脚本
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parent / "static"

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

# 文档页依赖 Swagger/Redoc 的 CDN 资源；CSP 只对业务与控制台生效
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def mount_console(app: FastAPI) -> None:
    """挂载静态控制台并添加根路径重定向。"""
    app.mount("/console", StaticFiles(directory=_STATIC_DIR, html=True), name="console")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/console/")


async def security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """统一安全头：CSP/防嗅探/防嵌套/无 Referrer。"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if not request.url.path.startswith(_DOCS_PATHS):
        response.headers.setdefault("Content-Security-Policy", _CSP)
    return response
