"""全局 JWT 校验：白名单外请求必须带有效 access token。"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from common.errors import ErrorCode
from common.response import fail
from common.security import decode_token

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

# 前缀匹配（Swagger 静态资源等）
_PREFIX_WHITELIST = (
    "/docs/",
    "/redoc/",
)


def _is_whitelisted(path: str) -> bool:
    if path in _EXACT_WHITELIST:
        return True
    return any(path.startswith(p) for p in _PREFIX_WHITELIST)


class JwtAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if _is_whitelisted(path):
            return await call_next(request)

        auth = request.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "missing access token"),
            )
        token = auth[7:].strip()
        if not token:
            return JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "missing access token"),
            )
        try:
            payload = decode_token(token)
        except ValueError:
            return JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "invalid access token"),
            )
        if payload.get("type") != "access":
            return JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "token type must be access"),
            )
        sub = payload.get("sub")
        if not sub:
            return JSONResponse(
                status_code=401,
                content=fail(ErrorCode.UNAUTHORIZED, "invalid token subject"),
            )
        request.state.jwt_sub = sub
        return await call_next(request)
