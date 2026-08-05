from __future__ import annotations

from fastapi import APIRouter

from api.deps import AdminUser, DbSession
from api.schemas.users import CreateUserRequest, ResetPasswordRequest, UpdateUserRequest
from api.services import users as users_svc
from common.response import ok

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
async def users_list(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    items = await users_svc.list_users(db)
    return ok({"items": items})


@router.post("")
async def users_create(body: CreateUserRequest, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    item = await users_svc.create_user(
        db,
        username=body.username,
        password=body.password,
        display_name=body.displayName,
        email=body.email,
        department=body.department,
        phone=body.phone,
        role_codes=body.roleCodes,
        is_active=body.isActive,
        is_superuser=body.isSuperuser,
        remark=body.remark,
    )
    return ok({"item": item})


@router.get("/{user_id}")
async def users_get(user_id: int, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    user = await users_svc.get_user(db, user_id)
    return ok({"item": users_svc.to_user_item(user)})


@router.patch("/{user_id}")
async def users_update(
    user_id: int,
    body: UpdateUserRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    fields_set = set(body.model_fields_set)
    item = await users_svc.update_user(
        db,
        user_id=user_id,
        actor_id=admin.id,
        display_name=body.displayName,
        email=body.email,
        department=body.department,
        phone=body.phone,
        role_codes=body.roleCodes,
        is_active=body.isActive,
        is_superuser=body.isSuperuser,
        remark=body.remark,
        fields_set=fields_set,
    )
    return ok({"item": item})


@router.post("/{user_id}/reset-password")
async def users_reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    await users_svc.reset_password(db, user_id=user_id, password=body.password)
    return ok({"reset": True})


@router.delete("/{user_id}")
async def users_delete(user_id: int, db: DbSession, admin: AdminUser) -> dict:
    await users_svc.delete_user(db, user_id=user_id, actor_id=admin.id)
    return ok({"deleted": True})
