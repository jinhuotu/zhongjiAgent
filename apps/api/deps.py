from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from common.security import decode_token
from db.models.user import User
from db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    db: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AppError(ErrorCode.UNAUTHORIZED, "missing access token", status_code=401)
    try:
        payload = decode_token(credentials.credentials)
    except ValueError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "invalid access token", status_code=401) from exc
    if payload.get("type") != "access":
        raise AppError(ErrorCode.UNAUTHORIZED, "token type must be access", status_code=401)
    username = payload.get("sub")
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
