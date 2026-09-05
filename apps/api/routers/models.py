from __future__ import annotations

from fastapi import APIRouter, Query

from api.deps import AdminUser, CurrentUser, DbSession
from api.schemas.models import CreateModelConfigRequest, UpdateModelConfigRequest
from api.services.models import configs as configs_svc
from common.response import ok

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/runtime")
async def models_runtime(db: DbSession, user: CurrentUser) -> dict:
    """任意登录用户可读：当前启用的快/深/向量绑定（不含明文 Key）。"""
    _ = user
    return ok(await configs_svc.runtime_status(db))


@router.get("/options")
async def models_options(
    db: DbSession,
    user: CurrentUser,
    kind: str | None = Query(default=None),
    modelType: str | None = Query(default=None),
) -> dict:
    """任意登录用户可读：启用中的模型选项（供业务页下拉，不含 Key）。"""
    _ = user
    if kind and kind not in ("llm", "embedding"):
        kind = None
    items = await configs_svc.list_enabled_options(
        db, kind=kind, model_type=modelType
    )
    return ok({"items": items})


@router.get("")
async def models_list(
    db: DbSession,
    admin: AdminUser,
    kind: str | None = Query(default=None),
) -> dict:
    _ = admin
    if kind and kind not in ("llm", "embedding"):
        kind = None
    items = await configs_svc.list_configs(db, kind=kind)
    return ok({"items": items})


@router.post("")
async def models_create(body: CreateModelConfigRequest, db: DbSession, admin: AdminUser) -> dict:
    item = await configs_svc.create_config(
        db,
        name=body.name,
        kind=body.kind,
        model_type=body.modelType,
        api_base=body.apiBase,
        api_key=body.apiKey,
        model_name=body.modelName,
        temperature=body.temperature,
        timeout_seconds=body.timeoutSeconds,
        embedding_dim=body.embeddingDim,
        remark=body.remark,
        enabled=body.enabled,
        scope_fast=body.scopeFast,
        scope_deep=body.scopeDeep,
        scope_embedding=body.scopeEmbedding,
        created_by=admin.id,
    )
    return ok({"item": item})


@router.patch("/{model_id}")
async def models_update(
    model_id: str,
    body: UpdateModelConfigRequest,
    db: DbSession,
    admin: AdminUser,
) -> dict:
    _ = admin
    item = await configs_svc.update_config(
        db,
        public_id=model_id,
        name=body.name,
        model_type=body.modelType,
        api_base=body.apiBase,
        api_key=body.apiKey,
        model_name=body.modelName,
        temperature=body.temperature,
        clear_temperature=body.clearTemperature,
        timeout_seconds=body.timeoutSeconds,
        embedding_dim=body.embeddingDim,
        remark=body.remark,
        enabled=body.enabled,
        scope_fast=body.scopeFast,
        scope_deep=body.scopeDeep,
        scope_embedding=body.scopeEmbedding,
    )
    return ok({"item": item})


@router.delete("/{model_id}")
async def models_delete(model_id: str, db: DbSession, admin: AdminUser) -> dict:
    _ = admin
    await configs_svc.delete_config(db, public_id=model_id)
    return ok({"deleted": True})
