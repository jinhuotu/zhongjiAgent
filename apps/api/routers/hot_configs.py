from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from api.deps import AdminUser, CurrentUser, DbSession
from api.services import hot_config as hot_svc
from common.response import ok

router = APIRouter(prefix="/hot-configs", tags=["hot-configs"])


class UpsertHotConfigRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = ""
    description: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    remark: str | None = Field(default=None, max_length=512)


@router.get("")
async def hot_configs_list(db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    await hot_svc.ensure_default_prompts(db)
    return ok({"items": await hot_svc.list_configs(db)})


@router.get("/value/{config_key}")
async def hot_config_get_value(config_key: str, db: DbSession, user: CurrentUser) -> dict:
    """任意登录用户可读单个配置值（走 Redis 热缓存）。"""
    _ = user
    value = await hot_svc.get_value(db, config_key)
    return ok({"key": config_key, "value": value})


@router.put("")
async def hot_configs_upsert(
    body: UpsertHotConfigRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    item = await hot_svc.upsert_config(
        db,
        config_key=body.key,
        value=body.value,
        description=body.description,
        enabled=body.enabled,
        operator_id=admin.id,
        remark=body.remark,
    )
    return ok({"item": item})


@router.delete("/{config_key}")
async def hot_configs_delete(config_key: str, db: DbSession, admin: AdminUser) -> dict:
    await hot_svc.delete_config(db, config_key=config_key, operator_id=admin.id)
    return ok({"deleted": True})


@router.get("/audits")
async def hot_configs_audits(
    db: DbSession,
    admin: AdminUser,
    key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    _ = admin
    return ok({"items": await hot_svc.list_audits(db, config_key=key, limit=limit)})
