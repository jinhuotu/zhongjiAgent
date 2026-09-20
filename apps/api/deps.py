from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from common.security import decode_token
from db.models.user import User
from db.session import get_db

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    request: Request,
    db: DbSession,
) -> User:
    """解析 access token。不走 HTTPBearer，避免 multipart / <video src> 被误判未登录。"""
    from api.middleware.auth import access_token_from_request

    username = str(getattr(request.state, "jwt_sub", "") or "").strip()
    if not username:
        raw = access_token_from_request(request)
        if not raw:
            raise AppError(ErrorCode.UNAUTHORIZED, "missing access token", status_code=401)
        try:
            payload = decode_token(raw)
        except ValueError as exc:
            raise AppError(ErrorCode.UNAUTHORIZED, "invalid access token", status_code=401) from exc
        if payload.get("type") != "access":
            raise AppError(ErrorCode.UNAUTHORIZED, "token type must be access", status_code=401)
        username = str(payload.get("sub") or "").strip()
    if not username:
        raise AppError(ErrorCode.UNAUTHORIZED, "invalid token subject", status_code=401)
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AppError(ErrorCode.UNAUTHORIZED, "user not found or inactive", status_code=401)
    return user


def user_is_admin(user: User) -> bool:
    if user.is_superuser:
        return True
    return any(getattr(r, "code", None) == "admin" for r in (user.roles or []))


async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user_is_admin(user):
        raise AppError(ErrorCode.FORBIDDEN, "admin only", status_code=403)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]


async def get_request_id(
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> str:
    return x_request_id or ""
