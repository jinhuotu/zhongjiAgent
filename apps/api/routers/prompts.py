from __future__ import annotations

from fastapi import APIRouter

from api.deps import AdminUser, CurrentUser, DbSession
from api.schemas.prompts import CreatePromptRequest, UpdatePromptRequest
from api.services.prompts import configs as configs_svc
from common.response import ok

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("/options")
async def prompts_options(db: DbSession, user: CurrentUser) -> dict:
    """任意登录用户：对话页可选启用提示词（不含全文）。"""
    _ = user
    return ok({"items": await configs_svc.list_options(db)})


@router.get("")
async def prompts_list(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    return ok({"items": await configs_svc.list_configs(db)})


@router.post("")
async def prompts_create(body: CreatePromptRequest, db: DbSession, admin: AdminUser) -> dict:
    item = await configs_svc.create_config(
        db,
        name=body.name,
        content=body.content,
        remark=body.remark,
        enabled=body.enabled,
        created_by=admin.id,
    )
    return ok({"item": item})


@router.patch("/{prompt_id}")
async def prompts_update(
    prompt_id: str,
    body: UpdatePromptRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    item = await configs_svc.update_config(
        db,
        public_id=prompt_id,
        name=body.name,
        content=body.content,
        remark=body.remark,
        enabled=body.enabled,
    )
    return ok({"item": item})


@router.delete("/{prompt_id}")
async def prompts_delete(prompt_id: str, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    await configs_svc.delete_config(db, public_id=prompt_id)
    return ok({"deleted": True})
