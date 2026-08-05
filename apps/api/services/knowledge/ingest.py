from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.knowledge.bases import get_base_by_public_id
from api.services.knowledge.chunking import split_text
from api.services.knowledge.embeddings import get_embedding_client
from api.services.knowledge.qdrant_store import get_qdrant_store
from common.errors import AppError, ErrorCode
from db.models.knowledge import KnowledgeDocument


def short_id(n: int = 10) -> str:
    return secrets.token_hex(n)[:n]


def to_kb_item(doc: KnowledgeDocument) -> dict[str, Any]:
    created_ms = int(doc.created_at.timestamp() * 1000) if doc.created_at else 0
    file_key = doc.file_key or doc.storage_path
    return {
        "id": doc.public_id,
        "baseId": getattr(doc, "_base_public_id", None),
        "name": doc.name,
        "source": doc.source,
        "kind": doc.kind,
        "fileType": doc.file_type,
        "size": doc.size,
        "url": doc.url,
        "fileKey": file_key,
        "previewUrl": doc.preview_url,
        "summary": doc.summary,
        "charCount": doc.char_count,
        "chunks": doc.chunk_count,
        "tags": doc.tags or [],
        "uploader": doc.uploader,
        "status": doc.status,
        "errorMsg": doc.error_msg,
        "createdAt": created_ms,
    }


async def list_documents(db: AsyncSession, *, base_public_id: str) -> list[dict[str, Any]]:
    base = await get_base_by_public_id(db, base_public_id)
    result = await db.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.base_id == base.id)
        .order_by(KnowledgeDocument.created_at.desc())
    )
    docs = result.scalars().all()
    items: list[dict[str, Any]] = []
    for d in docs:
        setattr(d, "_base_public_id", base.public_id)
        items.append(to_kb_item(d))
    return items


async def ingest_text(
    db: AsyncSession,
    *,
    base_public_id: str,
    name: str,
    content: str,
    source: str = "text",
    file_type: str | None = "txt",
    size: int | None = None,
    url: str | None = None,
    file_key: str | None = None,
    preview_url: str | None = None,
    kind: str = "doc",
    tags: list[str] | None = None,
    uploader: str | None = None,
    created_by: int | None = None,
) -> dict[str, Any]:
    text = content.strip()
    if len(text) < 4:
        raise AppError(ErrorCode.VALIDATION, "content too short", status_code=422)

    base = await get_base_by_public_id(db, base_public_id)
    public_id = short_id(12)
    doc = KnowledgeDocument(
        public_id=public_id,
        base_id=base.id,
        name=name.strip() or "untitled",
        source=source,
        kind=kind,
        file_type=file_type,
        size=size if size is not None else len(text.encode("utf-8")),
        url=url,
        storage_path=file_key,
        file_key=file_key,
        preview_url=preview_url,
        summary=text[:200],
        char_count=len(text),
        chunk_count=0,
        tags=tags or [],
        uploader=uploader,
        status="parsing",
        created_by=created_by,
    )
    db.add(doc)
    await db.flush()

    try:
        chunks = split_text(text)
        embedder = await get_embedding_client(db)
        vectors = await embedder.embed_documents(chunks)
        store = get_qdrant_store()
        store.upsert_chunks(
            public_id=public_id,
            kb_id=base.public_id,
            name=doc.name,
            source=doc.source,
            tags=doc.tags if isinstance(doc.tags, list) else [],
            chunks=chunks,
            vectors=vectors,
        )
        doc.chunk_count = len(chunks)
        doc.status = "ready"
        doc.error_msg = None
    except Exception as exc:  # noqa: BLE001
        doc.status = "failed"
        doc.error_msg = str(exc)
        await db.commit()
        await db.refresh(doc)
        if isinstance(exc, AppError):
            raise
        raise AppError(ErrorCode.INTERNAL, f"ingest failed: {exc}", status_code=500) from exc

    await db.commit()
    await db.refresh(doc)
    setattr(doc, "_base_public_id", base.public_id)
    return to_kb_item(doc)


async def get_document_preview(
    db: AsyncSession,
    *,
    base_public_id: str,
    doc_public_id: str,
) -> dict[str, Any]:
    """返回文档元数据 + 向量库文本块拼接预览。"""
    base = await get_base_by_public_id(db, base_public_id)
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.public_id == doc_public_id,
            KnowledgeDocument.base_id == base.id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND, "document not found", status_code=404)

    setattr(doc, "_base_public_id", base.public_id)
    chunks: list[dict[str, Any]] = []
    try:
        chunks = get_qdrant_store().list_chunks_by_doc_id(doc_public_id)
    except Exception:  # noqa: BLE001
        chunks = []

    parts = [str(c.get("content") or "") for c in chunks if str(c.get("content") or "").strip()]
    content = "\n\n".join(parts).strip()
    truncated = False
    if not content and doc.summary:
        content = doc.summary
        truncated = True

    return {
        "item": to_kb_item(doc),
        "chunks": chunks,
        "content": content,
        "truncated": truncated,
    }


async def search_chunks(
    db: AsyncSession,
    *,
    query: str,
    top_k: int = 5,
    min_score: float = 0.0,
    kb_id: str | None = None,
    kb_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    from common.config import get_settings
    from api.services.knowledge.rerank import hybrid_rerank

    q = query.strip()
    if not q:
        raise AppError(ErrorCode.VALIDATION, "query is required", status_code=422)

    settings = get_settings()
    embedder = await get_embedding_client(db)
    vector = await embedder.embed_query(q)
    store = get_qdrant_store()

    # 多召回一些候选，再关键词重排，避免「语义近但关键词不中」的块占住 top1
    mult = max(1, int(settings.kb_search_candidate_multiplier or 1))
    candidate_k = max(top_k, top_k * mult)
    hits = store.search(
        vector=vector,
        top_k=candidate_k,
        min_score=min_score,
        kb_id=kb_id,
        kb_ids=kb_ids,
    )
    return hybrid_rerank(
        q,
        hits,
        top_k=top_k,
        keyword_weight=float(settings.kb_search_keyword_weight),
    )
