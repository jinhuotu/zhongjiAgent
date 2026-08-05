from __future__ import annotations

from fastapi import APIRouter

from api.deps import AdminUser, DbSession
from api.schemas.roles import CreateRoleRequest, UpdateRoleRequest
from api.services import users as users_svc
from common.response import ok

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("")
async def roles_list(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    items = await users_svc.list_roles(db)
    return ok({"items": items})


@router.post("")
async def roles_create(body: CreateRoleRequest, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    item = await users_svc.create_role(
        db,
        name=body.name,
        description=body.description,
    )
    return ok({"item": item})


@router.patch("/{role_id}")
async def roles_update(
    role_id: int,
    body: UpdateRoleRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    item = await users_svc.update_role(
        db,
        role_id=role_id,
        name=body.name,
        description=body.description,
        fields_set=set(body.model_fields_set),
    )
    return ok({"item": item})


@router.delete("/{role_id}")
async def roles_delete(role_id: int, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    await users_svc.delete_role(db, role_id=role_id)
    return ok({"deleted": True})
