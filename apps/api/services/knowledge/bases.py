from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from common.errors import AppError, ErrorCode
from db.models.knowledge import KnowledgeBase, KnowledgeDocument


def short_id(n: int = 12) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def to_base_item(base: KnowledgeBase, *, doc_count: int | None = None, chunk_count: int | None = None) -> dict[str, Any]:
    docs = list(base.documents) if base.documents is not None else []
    if doc_count is None:
        doc_count = len(docs)
    if chunk_count is None:
        chunk_count = sum(int(d.chunk_count or 0) for d in docs)
    created_ms = int(base.created_at.timestamp() * 1000) if base.created_at else 0
    updated_ms = int(base.updated_at.timestamp() * 1000) if base.updated_at else created_ms
    return {
        "id": base.public_id,
        "name": base.name,
        "description": base.description,
        "status": base.status,
        "docCount": doc_count,
        "chunkCount": chunk_count,
        "createdAt": created_ms,
        "updatedAt": updated_ms,
    }


async def get_base_by_public_id(db: AsyncSession, public_id: str) -> KnowledgeBase:
    result = await db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.public_id == public_id)
        .options(selectinload(KnowledgeBase.documents))
    )
    base = result.scalar_one_or_none()
    if base is None:
        raise AppError(ErrorCode.NOT_FOUND, "knowledge base not found", status_code=404)
    return base


async def list_bases(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.status != "deleted")
        .options(selectinload(KnowledgeBase.documents))
        .order_by(KnowledgeBase.updated_at.desc())
    )
    bases = result.scalars().all()
    return [to_base_item(b) for b in bases]


async def create_base(
    db: AsyncSession,
    *,
    name: str,
    description: str | None = None,
    created_by: int | None = None,
) -> dict[str, Any]:
    title = name.strip()
    if not title:
        raise AppError(ErrorCode.VALIDATION, "name is required", status_code=422)
    base = KnowledgeBase(
        public_id=short_id(12),
        name=title,
        description=(description or "").strip() or None,
        status="active",
        created_by=created_by,
    )
    db.add(base)
    await db.commit()
    await db.refresh(base)
    return to_base_item(base, doc_count=0, chunk_count=0)


async def update_base(
    db: AsyncSession,
    *,
    public_id: str,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    base = await get_base_by_public_id(db, public_id)
    if name is not None:
        title = name.strip()
        if not title:
            raise AppError(ErrorCode.VALIDATION, "name is required", status_code=422)
        base.name = title
    if description is not None:
        base.description = description.strip() or None
    await db.commit()
    await db.refresh(base)
    return to_base_item(base)


async def delete_base(db: AsyncSession, *, public_id: str) -> dict[str, Any]:
    """级联删除库内文档元数据；向量点由调用方按 doc_id 清理。"""
    base = await get_base_by_public_id(db, public_id)
    item = to_base_item(base)
    doc_ids = [d.public_id for d in (base.documents or [])]
    await db.delete(base)
    await db.commit()
    return {"base": item, "deletedDocIds": doc_ids}


async def count_docs(db: AsyncSession, base_pk: int) -> int:
    result = await db.execute(
        select(func.count()).select_from(KnowledgeDocument).where(KnowledgeDocument.base_id == base_pk)
    )
    return int(result.scalar_one() or 0)
