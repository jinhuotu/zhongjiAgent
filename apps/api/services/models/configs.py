from __future__ import annotations

import secrets
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from db.models.model_config import ModelConfig

Kind = Literal["llm", "embedding"]


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def mask_api_key(key: str) -> str:
    raw = (key or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{raw[:4]}{'*' * min(12, len(raw) - 8)}{raw[-4:]}"


def to_item(cfg: ModelConfig, *, reveal_key: bool = False) -> dict[str, Any]:
    return {
        "id": cfg.public_id,
        "name": cfg.name,
        "kind": cfg.kind,
        "apiBase": cfg.api_base,
        "apiKeyMasked": mask_api_key(cfg.api_key),
        "apiKey": cfg.api_key if reveal_key else None,
        "modelName": cfg.model_name,
        "temperature": cfg.temperature,
        "timeoutSeconds": cfg.timeout_seconds,
        "embeddingDim": cfg.embedding_dim,
        "remark": cfg.remark,
        "enabled": bool(cfg.enabled),
        "scopeFast": bool(cfg.scope_fast),
        "scopeDeep": bool(cfg.scope_deep),
        "scopeEmbedding": bool(cfg.scope_embedding),
        "createdAt": int(cfg.created_at.timestamp() * 1000) if cfg.created_at else 0,
        "updatedAt": int(cfg.updated_at.timestamp() * 1000) if cfg.updated_at else 0,
    }


async def list_configs(
    db: AsyncSession,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(ModelConfig).order_by(ModelConfig.updated_at.desc())
    if kind:
        stmt = stmt.where(ModelConfig.kind == kind)
    result = await db.execute(stmt)
    return [to_item(c) for c in result.scalars().all()]


async def get_by_public_id(db: AsyncSession, public_id: str) -> ModelConfig:
    result = await db.execute(select(ModelConfig).where(ModelConfig.public_id == public_id))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise AppError(ErrorCode.NOT_FOUND, "model config not found", status_code=404)
    return cfg


async def _clear_scope(db: AsyncSession, *, kind: str, scope: str) -> None:
    values: dict[str, Any] = {}
    if scope == "fast":
        values["scope_fast"] = False
    elif scope == "deep":
        values["scope_deep"] = False
    elif scope == "embedding":
        values["scope_embedding"] = False
    else:
        return
    await db.execute(update(ModelConfig).where(ModelConfig.kind == kind).values(**values))


async def create_config(
    db: AsyncSession,
    *,
    name: str,
    kind: Kind,
    api_base: str,
    api_key: str,
    model_name: str,
    temperature: float | None = None,
    timeout_seconds: float = 120.0,
    embedding_dim: int | None = None,
    remark: str | None = None,
    enabled: bool = True,
    scope_fast: bool = False,
    scope_deep: bool = False,
    scope_embedding: bool = False,
    created_by: int | None = None,
) -> dict[str, Any]:
    if kind not in ("llm", "embedding"):
        raise AppError(ErrorCode.VALIDATION, "kind must be llm or embedding", status_code=422)
    if not name.strip() or not api_base.strip() or not api_key.strip() or not model_name.strip():
        raise AppError(ErrorCode.VALIDATION, "name/apiBase/apiKey/modelName required", status_code=422)
    if kind == "embedding" and (scope_fast or scope_deep):
        raise AppError(ErrorCode.VALIDATION, "embedding cannot bind chat scopes", status_code=422)
    if kind == "llm" and scope_embedding:
        raise AppError(ErrorCode.VALIDATION, "llm cannot bind embedding scope", status_code=422)

    if scope_fast:
        await _clear_scope(db, kind="llm", scope="fast")
    if scope_deep:
        await _clear_scope(db, kind="llm", scope="deep")
    if scope_embedding:
        await _clear_scope(db, kind="embedding", scope="embedding")

    cfg = ModelConfig(
        public_id=short_id(12),
        name=name.strip()[:128],
        kind=kind,
        api_base=api_base.strip().rstrip("/"),
        api_key=api_key.strip(),
        model_name=model_name.strip()[:128],
        temperature=temperature,
        timeout_seconds=float(timeout_seconds or 120),
        embedding_dim=embedding_dim,
        remark=(remark or "").strip()[:512] or None,
        enabled=enabled,
        scope_fast=bool(scope_fast) if kind == "llm" else False,
        scope_deep=bool(scope_deep) if kind == "llm" else False,
        scope_embedding=bool(scope_embedding) if kind == "embedding" else False,
        created_by=created_by,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return to_item(cfg)


async def update_config(
    db: AsyncSession,
    *,
    public_id: str,
    name: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    timeout_seconds: float | None = None,
    embedding_dim: int | None = None,
    remark: str | None = None,
    enabled: bool | None = None,
    scope_fast: bool | None = None,
    scope_deep: bool | None = None,
    scope_embedding: bool | None = None,
    clear_temperature: bool = False,
) -> dict[str, Any]:
    cfg = await get_by_public_id(db, public_id)
    if name is not None:
        if not name.strip():
            raise AppError(ErrorCode.VALIDATION, "name required", status_code=422)
        cfg.name = name.strip()[:128]
    if api_base is not None:
        cfg.api_base = api_base.strip().rstrip("/")
    if api_key is not None and api_key.strip() and "*" not in api_key:
        cfg.api_key = api_key.strip()
    if model_name is not None:
        cfg.model_name = model_name.strip()[:128]
    if clear_temperature:
        cfg.temperature = None
    elif temperature is not None:
        cfg.temperature = temperature
    if timeout_seconds is not None:
        cfg.timeout_seconds = float(timeout_seconds)
    if embedding_dim is not None:
        cfg.embedding_dim = embedding_dim
    if remark is not None:
        cfg.remark = remark.strip()[:512] or None
    if enabled is not None:
        cfg.enabled = enabled

    if cfg.kind == "llm":
        if scope_fast is True:
            await _clear_scope(db, kind="llm", scope="fast")
            cfg.scope_fast = True
        elif scope_fast is False:
            cfg.scope_fast = False
        if scope_deep is True:
            await _clear_scope(db, kind="llm", scope="deep")
            cfg.scope_deep = True
        elif scope_deep is False:
            cfg.scope_deep = False
        cfg.scope_embedding = False
    else:
        if scope_embedding is True:
            await _clear_scope(db, kind="embedding", scope="embedding")
            cfg.scope_embedding = True
        elif scope_embedding is False:
            cfg.scope_embedding = False
        cfg.scope_fast = False
        cfg.scope_deep = False

    await db.commit()
    await db.refresh(cfg)
    return to_item(cfg)


async def delete_config(db: AsyncSession, *, public_id: str) -> None:
    cfg = await get_by_public_id(db, public_id)
    await db.delete(cfg)
    await db.commit()


async def resolve_active_llm(db: AsyncSession, mode: str) -> ModelConfig | None:
    scope = ModelConfig.scope_deep if mode == "deep" else ModelConfig.scope_fast
    result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.kind == "llm",
            ModelConfig.enabled.is_(True),
            scope.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def resolve_active_embedding(db: AsyncSession) -> ModelConfig | None:
    result = await db.execute(
        select(ModelConfig).where(
            ModelConfig.kind == "embedding",
            ModelConfig.enabled.is_(True),
            ModelConfig.scope_embedding.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def runtime_status(db: AsyncSession) -> dict[str, Any]:
    fast = await resolve_active_llm(db, "fast")
    deep = await resolve_active_llm(db, "deep")
    emb = await resolve_active_embedding(db)
    return {
        "llm_fast": to_item(fast) if fast else None,
        "llm_deep": to_item(deep) if deep else None,
        "embedding": to_item(emb) if emb else None,
        "llm_configured": bool(fast or deep),
        "embedding_configured": bool(emb),
    }
