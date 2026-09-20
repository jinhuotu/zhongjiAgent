from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.knowledge.bases import get_base_by_public_id
from api.services.knowledge.chunking import split_text
from api.services.knowledge.embeddings import get_embedding_client
from api.services.knowledge.qdrant_store import get_qdrant_store
from api.services.knowledge.image_contract import KIND_IMAGE, is_image_ext
from api.services.knowledge.video_contract import KIND_VIDEO, is_video_ext
from common.config import get_settings
from common.errors import AppError, ErrorCode
from db.models.knowledge import KnowledgeBase, KnowledgeDocument


def short_id(n: int = 10) -> str:
    return secrets.token_hex(n)[:n]


def _storage_root() -> Path:
    return Path(get_settings().storage_root).expanduser().resolve()


def resolve_storage_path(key: str) -> Path:
    rel = (key or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid storage key", status_code=400)
    root = _storage_root()
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid storage key", status_code=400)
    return path


def unlink_stored_file(key: str | None) -> None:
    if not key:
        return
    try:
        path = resolve_storage_path(key)
    except AppError:
        return
    if path.is_file():
        path.unlink(missing_ok=True)


def _extracted_text_key(base_public_id: str, doc_public_id: str) -> str:
    return f"knowledge/{base_public_id}/{doc_public_id}.txt"


def write_extracted_text(base_public_id: str, doc_public_id: str, text: str) -> None:
    path = resolve_storage_path(_extracted_text_key(base_public_id, doc_public_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_extracted_text(base_public_id: str, doc_public_id: str) -> str | None:
    try:
        path = resolve_storage_path(_extracted_text_key(base_public_id, doc_public_id))
    except AppError:
        return None
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    return raw or None


def _error_msg(exc: BaseException) -> str:
    if isinstance(exc, AppError):
        return (exc.msg or str(exc))[:500]
    return str(exc)[:500]


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


async def _vectorize_document(
    db: AsyncSession,
    doc: KnowledgeDocument,
    text: str,
    *,
    timed_chunks: list | None = None,
) -> None:
    cleaned = text.strip()
    if len(cleaned) < 4:
        raise AppError(ErrorCode.VALIDATION, "content too short", status_code=422)

    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == doc.base_id))
    base = result.scalar_one_or_none()
    if base is None:
        raise AppError(ErrorCode.NOT_FOUND, "knowledge base not found", status_code=404)

    meta: list[dict[str, Any]] | None = None
    if timed_chunks:
        pieces = [c for c in timed_chunks if getattr(c, "content", None)]
        chunks = [str(c.content).strip() for c in pieces if str(c.content).strip()]
        meta = []
        for c in pieces:
            if not str(c.content).strip():
                continue
            row: dict[str, Any] = {}
            if getattr(c, "start_ms", None) is not None:
                row["startMs"] = int(c.start_ms)
            if getattr(c, "end_ms", None) is not None:
                row["endMs"] = int(c.end_ms)
            meta.append(row)
        if not any(row for row in meta):
            meta = None
    else:
        chunks = split_text(cleaned)

    if not chunks:
        raise AppError(ErrorCode.VALIDATION, "content too short after chunk", status_code=422)

    embedder = await get_embedding_client(db)
    vectors = await embedder.embed_documents(chunks)
    store = get_qdrant_store()
    store.delete_by_doc_id(doc.public_id)
    store.upsert_chunks(
        public_id=doc.public_id,
        kb_id=base.public_id,
        name=doc.name,
        source=doc.source,
        tags=doc.tags if isinstance(doc.tags, list) else [],
        chunks=chunks,
        vectors=vectors,
        chunk_meta=meta,
    )
    doc.char_count = len(cleaned)
    doc.chunk_count = len(chunks)
    if not (doc.summary or "").startswith("PERFJSON:"):
        doc.summary = cleaned[:200]
    doc.status = "ready"
    doc.error_msg = None


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
        await _vectorize_document(db, doc, text)
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

    sidecar = read_extracted_text(base.public_id, doc_public_id)
    parts = [str(c.get("content") or "") for c in chunks if str(c.get("content") or "").strip()]
    content = "\n\n".join(parts).strip()
    truncated = False
    if sidecar and (not content or len(sidecar) > len(content)):
        content = sidecar
    if not content and doc.summary:
        content = doc.summary
        truncated = True

    return {
        "item": to_kb_item(doc),
        "chunks": chunks,
        "content": content,
        "truncated": truncated,
        "hasFile": bool((doc.file_key or doc.storage_path or "").strip()),
    }


_FILE_MIME = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def _guess_file_mime(path: Path) -> str:
    return _FILE_MIME.get(path.suffix.lower(), "application/octet-stream")


async def resolve_document_file(
    db: AsyncSession,
    *,
    base_public_id: str,
    doc_public_id: str,
) -> tuple[Path, str, str]:
    """原件落盘路径、下载名、MIME。无原件抛 404（不做 txt 回退）。"""
    base = await get_base_by_public_id(db, base_public_id)
    result = await db.execute(
        select(KnowledgeDocument).where(
            KnowledgeDocument.public_id == doc_public_id,
            KnowledgeDocument.base_id == base.id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise AppError(ErrorCode.NOT_FOUND, "文档不存在", status_code=404)

    key = doc.file_key or doc.storage_path
    if not key:
        raise AppError(ErrorCode.NOT_FOUND, "文档无原文件", status_code=404)
    try:
        path = resolve_storage_path(key)
    except AppError as exc:
        raise AppError(ErrorCode.NOT_FOUND, "文档无原文件", status_code=404) from exc
    if not path.is_file():
        raise AppError(ErrorCode.NOT_FOUND, "文档无原文件", status_code=404)

    stem = (doc.name or path.stem or doc.public_id).replace("/", "_").replace("\\", "_")
    suffix = path.suffix or ""
    if suffix and not stem.lower().endswith(suffix.lower()):
        name = f"{stem}{suffix}"
    else:
        name = path.name or f"{stem}.bin"
    return path, name, _guess_file_mime(path)


async def download_document(
    db: AsyncSession,
    *,
    base_public_id: str,
    doc_public_id: str,
) -> tuple[bytes, str, str]:
    """优先磁盘原文件；否则导出预览正文为 txt。"""
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

    key = doc.file_key or doc.storage_path
    if key:
        try:
            path = resolve_storage_path(key)
        except AppError:
            path = None
        else:
            if path.is_file():
                name = path.name or f"{doc.name}.bin"
                return path.read_bytes(), name, _guess_file_mime(path)

    preview = await get_document_preview(
        db, base_public_id=base_public_id, doc_public_id=doc_public_id
    )
    content = str(preview.get("content") or doc.summary or doc.name or "")
    if not content.strip():
        raise AppError(40402, "文档无可导出内容", status_code=404)
    filename = f"{doc.name or doc.public_id}.txt".replace("/", "_").replace("\\", "_")
    return content.encode("utf-8"), filename, "text/plain; charset=utf-8"


_PREVIEW_VIDEO = frozenset({"mp4", "webm"})
_PREVIEW_IMAGE = frozenset({"png", "jpg", "jpeg", "webp", "gif", "bmp"})


def _chunk_preview_kind(file_type: str | None, *, has_file: bool) -> str:
    if not has_file:
        return ""
    ext = (file_type or "").lower().lstrip(".")
    if ext in _PREVIEW_VIDEO:
        return "video"
    if ext in _PREVIEW_IMAGE:
        return "image"
    return "file" if ext else ""


async def _fill_chunk_names(
    db: AsyncSession, hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """用 MySQL 资料名称覆盖/补全 Qdrant payload，聊天引用才带得上名字与视频元数据。"""
    ids = [str(h.get("doc_id") or "").strip() for h in hits]
    ids = [x for x in ids if x]
    if not ids:
        return hits
    result = await db.execute(
        select(
            KnowledgeDocument.public_id,
            KnowledgeDocument.name,
            KnowledgeDocument.file_type,
            KnowledgeDocument.file_key,
            KnowledgeDocument.storage_path,
            KnowledgeDocument.tags,
            KnowledgeDocument.kind,
        ).where(KnowledgeDocument.public_id.in_(ids))
    )
    rows = {
        str(pid): (
            name,
            (ft or "").strip().lower(),
            bool((file_key or storage_path or "").strip()),
            tags if isinstance(tags, list) else [],
            (kind or "").strip().lower(),
        )
        for pid, name, ft, file_key, storage_path, tags, kind in result.all()
    }
    out: list[dict[str, Any]] = []
    for h in hits:
        item = dict(h)
        pid = str(item.get("doc_id") or "").strip()
        name, ft, has_file, tags, kind = rows.get(pid, ("", "", False, [], ""))
        if name:
            item["name"] = name
        elif not str(item.get("name") or "").strip():
            item["name"] = "未命名资料"
        item["file_type"] = ft or str(item.get("file_type") or "")
        item["has_file"] = has_file
        if kind:
            item["kind"] = kind
        elif is_video_ext(item["file_type"]):
            item["kind"] = KIND_VIDEO
        elif is_image_ext(item["file_type"]):
            item["kind"] = KIND_IMAGE
        item["preview_kind"] = _chunk_preview_kind(item["file_type"], has_file=has_file)
        if item.get("startMs") is not None:
            try:
                item["startMs"] = int(item["startMs"])
            except (TypeError, ValueError):
                item.pop("startMs", None)
        if item.get("endMs") is not None:
            try:
                item["endMs"] = int(item["endMs"])
            except (TypeError, ValueError):
                item.pop("endMs", None)
        if not item.get("tags") and tags:
            item["tags"] = tags
        out.append(item)
    return out


async def search_chunks(
    db: AsyncSession,
    *,
    query: str,
    top_k: int = 5,
    min_score: float = 0.0,
    kb_id: str | None = None,
    kb_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
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
    ranked = hybrid_rerank(
        q,
        hits,
        top_k=top_k,
        keyword_weight=float(settings.kb_search_keyword_weight),
    )
    return await _fill_chunk_names(db, ranked)
