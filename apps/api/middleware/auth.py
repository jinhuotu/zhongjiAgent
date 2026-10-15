"""全局 JWT 校验：白名单外请求必须带有效 access token。

纯 ASGI 中间件（不用 BaseHTTPMiddleware），避免 SSE / 视频 Range 流被缓冲。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from common.errors import ErrorCode
from common.response import fail
from common.security import decode_token

# 白名单-精确匹配：健康检查/登录/刷新/docs 放行，无需 token
# 精确匹配
_EXACT_WHITELIST = frozenset(
    {
        "/health",
        "/api/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/openapi.json",
        "/docs",
        "/redoc",
    }
)

# 白名单-前缀匹配：Swagger 静态资源放行
# 前缀匹配（Swagger 静态资源等）
_PREFIX_WHITELIST = (
    "/docs/",
    "/redoc/",
)


def _is_whitelisted(path: str) -> bool:
    if path in _EXACT_WHITELIST:
        return True
    return any(path.startswith(p) for p in _PREFIX_WHITELIST)


def _query_token_allowed(path: str) -> bool:
    """<video src> / <img src> 无法带头，仅允许知识库原件流用 ?access_token=。"""
    p = path or ""
    return "/knowledge/documents/" in p and p.rstrip("/").endswith("/file")


def access_token_from_headers(headers) -> str:
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    extra = headers.get("X-Access-Token") or headers.get("x-access-token") or ""
    auth = str(auth or "")
    extra = str(extra or "").strip()
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token:
            return token
    return extra


# 取 token 统一入口：先看请求头，再看知识库原件 ?access_token=（<video>/<img> 发不出头）
def access_token_from_request(request: Request) -> str:
    token = access_token_from_headers(request.headers)
    if token:
        return token
    if not _query_token_allowed(request.url.path):
        return ""
    return (request.query_params.get("access_token") or "").strip()


# 纯 ASGI 网关：不用 BaseHTTPMiddleware，避免 SSE/视频 Range 流被缓冲卡死
class JwtAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        if request.method == "OPTIONS" or _is_whitelisted(request.url.path):
            await self.app(scope, receive, send)
            return

        # 校验三步：取 token -> 验签 -> 查 type/sub，失败回 401{code:40100}
        token = access_token_from_request(request)
        if not token:
            response = JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "missing access token"),
            )
            await response(scope, receive, send)
            return
        try:
            payload = decode_token(token)
        except ValueError:
            response = JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "invalid access token"),
            )
            await response(scope, receive, send)
            return
        if payload.get("type") != "access":
            response = JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "token type must be access"),
            )
            await response(scope, receive, send)
            return
        sub = payload.get("sub")
        if not sub:
            response = JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "invalid token subject"),
            )
            await response(scope, receive, send)
            return
        # 验签通过：把用户名存 state，供 deps.get_current_user 复用，免二次验签
        request.state.jwt_sub = sub
        await self.app(scope, receive, send)
