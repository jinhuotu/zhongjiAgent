from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload, selectinload

from common.errors import AppError, ErrorCode
from common.security import hash_password
from db.models.role import Role, UserRole
from db.models.user import User

_PROTECTED_ROLE_CODES = frozenset({"admin"})


async def _generate_unique_role_code(db: AsyncSession) -> str:
    """Generate opaque role code: role_<12 hex chars>."""
    for _ in range(12):
        code = f"role_{secrets.token_hex(6)}"
        exists = await db.execute(select(Role.id).where(Role.code == code))
        if exists.scalar_one_or_none() is None:
            return code
    raise AppError(ErrorCode.INTERNAL, "无法生成角色编码，请重试", status_code=500)


async def _role_user_count(db: AsyncSession, role_id: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(UserRole).where(UserRole.role_id == role_id)
    )
    return int(result.scalar_one())


def _ts_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_user_item(user: User) -> dict[str, Any]:
    roles = list(user.roles or [])
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "email": user.email,
        "department": user.department,
        "phone": user.phone,
        "isActive": bool(user.is_active),
        "isSuperuser": bool(user.is_superuser),
        "roles": [r.code for r in roles],
        "roleNames": [r.name for r in roles],
        "remark": user.remark,
        "lastLoginAt": _ts_ms(user.last_login_at),
        "createdAt": _ts_ms(user.created_at) or 0,
        "updatedAt": _ts_ms(user.updated_at) or 0,
    }


def to_role_item(role: Role, user_count: int = 0) -> dict[str, Any]:
    return {
        "id": role.id,
        "code": role.code,
        "name": role.name,
        "description": role.description,
        "userCount": user_count,
        "createdAt": _ts_ms(role.created_at) or 0,
        "updatedAt": _ts_ms(role.updated_at) or 0,
    }


async def list_roles(db: AsyncSession) -> list[dict[str, Any]]:
    count_sq = (
        select(UserRole.role_id, func.count(UserRole.user_id).label("cnt"))
        .group_by(UserRole.role_id)
        .subquery()
    )
    stmt = (
        select(Role, func.coalesce(count_sq.c.cnt, 0))
        .options(noload(Role.users))
        .outerjoin(count_sq, Role.id == count_sq.c.role_id)
        .order_by(Role.id.asc())
    )
    result = await db.execute(stmt)
    return [to_role_item(role, int(cnt)) for role, cnt in result.all()]


async def get_role(db: AsyncSession, role_id: int) -> Role:
    result = await db.execute(
        select(Role).options(noload(Role.users)).where(Role.id == role_id)
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise AppError(ErrorCode.NOT_FOUND, "角色不存在", status_code=404)
    return role


async def create_role(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise AppError(ErrorCode.VALIDATION, "请填写角色名称", status_code=422)

    code = await _generate_unique_role_code(db)
    role = Role(
        code=code,
        name=name,
        description=(description or "").strip() or None,
    )
    db.add(role)
    await db.flush()
    await db.commit()
    role = await get_role(db, role.id)
    return to_role_item(role, 0)


async def update_role(
    db: AsyncSession,
    *,
    role_id: int,
    name: str | None = None,
    description: str | None = None,
    fields_set: set[str] | None = None,
) -> dict[str, Any]:
    fields_set = fields_set or set()
    role = await get_role(db, role_id)

    if "name" in fields_set:
        new_name = (name or "").strip()
        if not new_name:
            raise AppError(ErrorCode.VALIDATION, "角色名称不能为空", status_code=422)
        role.name = new_name

    if "description" in fields_set:
        role.description = (description or "").strip() or None

    await db.commit()
    role = await get_role(db, role.id)
    return to_role_item(role, await _role_user_count(db, role.id))


async def delete_role(db: AsyncSession, *, role_id: int) -> None:
    role = await get_role(db, role_id)
    if role.code in _PROTECTED_ROLE_CODES:
        raise AppError(ErrorCode.BAD_REQUEST, "系统内置角色不可删除", status_code=400)
    count = await _role_user_count(db, role.id)
    if count > 0:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"仍有 {count} 名用户使用该角色，请先解除后再删除",
            status_code=400,
        )
    await db.delete(role)
    await db.commit()


async def _load_roles_by_codes(db: AsyncSession, codes: list[str]) -> list[Role]:
    if not codes:
        return []
    unique = list(dict.fromkeys(codes))
    result = await db.execute(select(Role).where(Role.code.in_(unique)))
    found = list(result.scalars().all())
    found_codes = {r.code for r in found}
    missing = [c for c in unique if c not in found_codes]
    if missing:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"未知角色：{', '.join(missing)}",
            status_code=400,
        )
    # preserve requested order
    by_code = {r.code: r for r in found}
    return [by_code[c] for c in unique]


async def list_users(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(User).options(selectinload(User.roles)).order_by(User.id.asc())
    )
    return [to_user_item(u) for u in result.scalars().all()]


async def get_user(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(
        select(User).options(selectinload(User.roles)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise AppError(ErrorCode.NOT_FOUND, "用户不存在", status_code=404)
    return user


async def create_user(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    display_name: str | None,
    email: str | None,
    department: str | None,
    phone: str | None,
    role_codes: list[str],
    is_active: bool,
    is_superuser: bool,
    remark: str | None,
) -> dict[str, Any]:
    username = username.strip()
    if not username:
        raise AppError(ErrorCode.VALIDATION, "请填写用户名", status_code=422)

    exists = await db.execute(select(User.id).where(User.username == username))
    if exists.scalar_one_or_none() is not None:
        raise AppError(ErrorCode.CONFLICT, "用户名已存在", status_code=409)

    if email:
        email = email.strip() or None
        if email:
            email_exists = await db.execute(select(User.id).where(User.email == email))
            if email_exists.scalar_one_or_none() is not None:
                raise AppError(ErrorCode.CONFLICT, "邮箱已被占用", status_code=409)

    roles = await _load_roles_by_codes(db, role_codes)
    user = User(
        username=username,
        hashed_password=hash_password(password),
        display_name=(display_name or "").strip() or None,
        email=email,
        department=(department or "").strip() or None,
        phone=(phone or "").strip() or None,
        is_active=is_active,
        is_superuser=is_superuser,
        remark=remark,
        roles=roles,
    )
    db.add(user)
    await db.flush()
    await db.commit()
    return to_user_item(await get_user(db, user.id))


async def update_user(
    db: AsyncSession,
    *,
    user_id: int,
    actor_id: int,
    display_name: str | None = None,
    email: str | None = None,
    department: str | None = None,
    phone: str | None = None,
    role_codes: list[str] | None = None,
    is_active: bool | None = None,
    is_superuser: bool | None = None,
    remark: str | None = None,
    fields_set: set[str] | None = None,
) -> dict[str, Any]:
    """fields_set: which request fields were explicitly provided (for nullable clears)."""
    fields_set = fields_set or set()
    user = await get_user(db, user_id)

    if "displayName" in fields_set:
        user.display_name = (display_name or "").strip() or None
    if "email" in fields_set:
        new_email = (email or "").strip() or None
        if new_email and new_email != user.email:
            email_exists = await db.execute(
                select(User.id).where(User.email == new_email, User.id != user.id)
            )
            if email_exists.scalar_one_or_none() is not None:
                raise AppError(ErrorCode.CONFLICT, "邮箱已被占用", status_code=409)
        user.email = new_email
    if "department" in fields_set:
        user.department = (department or "").strip() or None
    if "phone" in fields_set:
        user.phone = (phone or "").strip() or None
    if "remark" in fields_set:
        user.remark = remark

    if is_active is not None:
        if user.id == actor_id and not is_active:
            raise AppError(ErrorCode.BAD_REQUEST, "不能停用当前登录账号", status_code=400)
        user.is_active = is_active

    if is_superuser is not None:
        if user.id == actor_id and not is_superuser and user.is_superuser:
            raise AppError(
                ErrorCode.BAD_REQUEST,
                "不能取消自己的超级管理员标识",
                status_code=400,
            )
        user.is_superuser = is_superuser

    if role_codes is not None:
        user.roles = await _load_roles_by_codes(db, role_codes)

    await db.commit()
    return to_user_item(await get_user(db, user.id))


async def reset_password(db: AsyncSession, *, user_id: int, password: str) -> None:
    user = await get_user(db, user_id)
    user.hashed_password = hash_password(password)
    await db.commit()


def _is_admin_user(user: User) -> bool:
    if user.is_superuser:
        return True
    return any(getattr(r, "code", None) == "admin" for r in (user.roles or []))


async def delete_user(db: AsyncSession, *, user_id: int, actor_id: int) -> None:
    user = await get_user(db, user_id)
    if user.id == actor_id:
        raise AppError(ErrorCode.BAD_REQUEST, "不能删除当前登录账号", status_code=400)

    if _is_admin_user(user):
        result = await db.execute(
            select(User).options(selectinload(User.roles)).where(User.id != user.id)
        )
        others = result.scalars().all()
        if not any(_is_admin_user(u) for u in others):
            raise AppError(
                ErrorCode.BAD_REQUEST,
                "不能删除唯一的管理员账号",
                status_code=400,
            )

    await db.delete(user)
    await db.commit()


async def touch_last_login(db: AsyncSession, user: User) -> None:
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
