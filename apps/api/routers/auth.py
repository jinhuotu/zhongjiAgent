from fastapi import APIRouter, Request
from sqlalchemy import select

from api.deps import CurrentUser, DbSession
from api.schemas.auth import LoginRequest, RefreshRequest, TokenPair, UserInfo
from api.services import audit as audit_svc
from api.services import menus as menus_svc
from api.services import users as users_svc
from common.errors import AppError, ErrorCode
from common.response import ok
from common.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from db.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_info(user: User) -> UserInfo:
    return UserInfo(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        is_superuser=user.is_superuser,
        roles=[r.code for r in user.roles],
        menus=menus_svc.resolve_menus(user),
    )


@router.post("/login")
async def login(body: LoginRequest, request: Request, db: DbSession) -> dict:
    ip = audit_svc.client_ip(request)
    ua = audit_svc.user_agent(request)
    result = await db.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        await audit_svc.record_login(
            username=body.username,
            success=False,
            reason="invalid_credentials",
            ip=ip,
            user_agent_str=ua,
            user_id=user.id if user is not None else None,
        )
        raise AppError(ErrorCode.UNAUTHORIZED, "invalid username or password", status_code=401)
    if not user.is_active:
        await audit_svc.record_login(
            username=user.username,
            success=False,
            reason="inactive",
            ip=ip,
            user_agent_str=ua,
            user_id=user.id,
        )
        raise AppError(ErrorCode.FORBIDDEN, "user is inactive", status_code=403)

    await users_svc.touch_last_login(db, user)
    await audit_svc.record_login(
        username=user.username,
        success=True,
        reason="ok",
        ip=ip,
        user_agent_str=ua,
        user_id=user.id,
    )

    tokens = TokenPair(
        access_token=create_access_token(user.username),
        refresh_token=create_refresh_token(user.username),
    )
    return ok({**tokens.model_dump(), "user": _user_info(user).model_dump()})


@router.post("/refresh")
async def refresh(body: RefreshRequest) -> dict:
    try:
        payload = decode_token(body.refresh_token)
    except ValueError as exc:
        raise AppError(ErrorCode.UNAUTHORIZED, "invalid refresh token", status_code=401) from exc
    if payload.get("type") != "refresh":
        raise AppError(ErrorCode.UNAUTHORIZED, "token type must be refresh", status_code=401)
    subject = payload.get("sub")
    if not subject:
        raise AppError(ErrorCode.UNAUTHORIZED, "invalid token subject", status_code=401)
    tokens = TokenPair(
        access_token=create_access_token(subject),
        refresh_token=create_refresh_token(subject),
    )
    return ok(tokens.model_dump())


@router.get("/me")
async def me(user: CurrentUser) -> dict:
    return ok(_user_info(user).model_dump())
