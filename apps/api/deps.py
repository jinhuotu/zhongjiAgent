from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from common.security import decode_token
from db.models.user import User
from db.session import get_db

# 每请求一个异步 DB 会话：前端每次 fetch 对应一次
DbSession = Annotated[AsyncSession, Depends(get_db)]


# 当前用户：先复用中间件的 jwt_sub，没有再从头/参数取 token 验签查库；禁用用户 401
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


# 管理员门禁：is_superuser 或有 admin 角色才过，否则 403
async def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user_is_admin(user):
        raise AppError(ErrorCode.FORBIDDEN, "admin only", status_code=403)
    return user


async def require_audit_reader(user: Annotated[User, Depends(get_current_user)]) -> User:
    """管理员或被授予「审计日志」菜单的角色（如 auditor）可只读访问。"""
    from api.services.menus import can_access_menu

    if user_is_admin(user) or can_access_menu(user, "/logs"):
        return user
    raise AppError(ErrorCode.FORBIDDEN, "audit reader only", status_code=403)


# 快捷别名：路由参数写 user: CurrentUser / AdminUser / AuditReader 即可鉴权
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
AuditReader = Annotated[User, Depends(require_audit_reader)]


async def get_request_id(
    request: Request,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
) -> str:
    rid = (x_request_id or "").strip()
    if rid:
        return rid[:64]
    return str(getattr(request.state, "request_id", "") or "")[:64]