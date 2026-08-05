from __future__ import annotations



import re



import httpx

from fastapi import APIRouter, Query

from sqlalchemy import select



from api.deps import CurrentUser, DbSession

from api.schemas.knowledge import (

    CreateBaseRequest,

    SearchRequest,

    TextDocumentRequest,

    UpdateBaseRequest,

    UploadDocumentRequest,

    UrlDocumentRequest,

)

from api.services.knowledge import bases as bases_svc

from api.services.knowledge.ingest import (
    get_document_preview,
    ingest_text,
    list_documents,
    search_chunks,
    to_kb_item,
)

from api.services.knowledge.qdrant_store import get_qdrant_store

from common.errors import AppError, ErrorCode

from common.response import ok

from db.models.knowledge import KnowledgeDocument



router = APIRouter(prefix="/knowledge", tags=["knowledge"])



_TAG_RE = re.compile(r"<[^>]+>")





def _html_to_text(raw: str) -> str:

    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)

    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)

    text = _TAG_RE.sub(" ", text)

    text = re.sub(r"\s+", " ", text)

    return text.strip()





# ---------- knowledge bases ----------





@router.get("/bases")

async def bases_list(db: DbSession, user: CurrentUser) -> dict:

    _ = user

    items = await bases_svc.list_bases(db)

    return ok({"items": items})





@router.post("/bases")

async def bases_create(body: CreateBaseRequest, db: DbSession, user: CurrentUser) -> dict:

    item = await bases_svc.create_base(

        db,

        name=body.name,

        description=body.description,

        created_by=user.id,

    )

    return ok({"item": item})





@router.get("/bases/{base_id}")

async def bases_get(base_id: str, db: DbSession, user: CurrentUser) -> dict:

    _ = user

    base = await bases_svc.get_base_by_public_id(db, base_id)

    return ok({"item": bases_svc.to_base_item(base)})





@router.patch("/bases/{base_id}")

async def bases_update(

    base_id: str,

    body: UpdateBaseRequest,

    db: DbSession,

    user: CurrentUser,

) -> dict:

    _ = user

    item = await bases_svc.update_base(

        db,

        public_id=base_id,

        name=body.name,

        description=body.description,

    )

    return ok({"item": item})





@router.delete("/bases/{base_id}")

async def bases_delete(base_id: str, db: DbSession, user: CurrentUser) -> dict:

    _ = user

    result = await bases_svc.delete_base(db, public_id=base_id)

    # Prefer payload filter by kb_id; also clean any legacy points without kb_id via doc ids.

    store = get_qdrant_store()

    try:

        store.delete_by_kb_id(base_id)

    except Exception:  # noqa: BLE001

        for doc_id in result.get("deletedDocIds") or []:

            store.delete_by_doc_id(doc_id)

    return ok(result)





# ---------- documents (scoped by baseId) ----------





@router.get("/documents")

async def documents_list(

    db: DbSession,

    user: CurrentUser,

    baseId: str = Query(..., min_length=1, max_length=32),

) -> dict:

    _ = user

    items = await list_documents(db, base_public_id=baseId)

    return {"code": 0, "msg": "ok", "data": {"items": items}, "items": items}





@router.post("/documents/upload")

async def documents_upload(

    body: UploadDocumentRequest,

    db: DbSession,

    user: CurrentUser,

) -> dict:

    item = await ingest_text(

        db,

        base_public_id=body.baseId,

        name=body.name,

        content=body.content,

        source="file",

        file_type=(body.fileType or "").lower() or "txt",

        size=body.size,

        tags=body.tags or ["手动上传"],

        uploader=body.uploader or user.display_name or user.username,

        created_by=user.id,

    )

    items = await list_documents(db, base_public_id=body.baseId)

    return {

        "code": 0,

        "msg": "ok",

        "data": {"item": item, "items": items},

        "item": item,

        "items": items,

    }





@router.post("/documents/from-text")

async def documents_from_text(

    body: TextDocumentRequest,

    db: DbSession,

    user: CurrentUser,

) -> dict:

    item = await ingest_text(

        db,

        base_public_id=body.baseId,

        name=body.title,

        content=body.content,

        source="text",

        file_type="txt",

        tags=body.tags or [],

        uploader=body.uploader or user.display_name or user.username,

        created_by=user.id,

    )

    items = await list_documents(db, base_public_id=body.baseId)

    return {

        "code": 0,

        "msg": "ok",

        "data": {"item": item, "items": items},

        "item": item,

        "items": items,

    }





@router.post("/documents/from-url")

async def documents_from_url(

    body: UrlDocumentRequest,

    db: DbSession,

    user: CurrentUser,

) -> dict:

    url = body.url.strip()

    try:

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:

            resp = await client.get(url, headers={"User-Agent": "zhongji-agent-kb/0.1"})

            resp.raise_for_status()

            raw = resp.text

    except Exception as exc:  # noqa: BLE001

        raise AppError(ErrorCode.BAD_REQUEST, f"fetch url failed: {exc}", status_code=400) from exc



    content = _html_to_text(raw)

    if len(content) < 4:

        raise AppError(ErrorCode.VALIDATION, "url content too short after parse", status_code=422)



    item = await ingest_text(

        db,

        base_public_id=body.baseId,

        name=(body.title or url).strip(),

        content=content[:200_000],

        source="url",

        file_type="html",

        url=url,

        size=len(content.encode("utf-8")),

        tags=body.tags or ["URL"],

        uploader=body.uploader or user.display_name or user.username,

        created_by=user.id,

    )

    items = await list_documents(db, base_public_id=body.baseId)

    return {

        "code": 0,

        "msg": "ok",

        "data": {"item": item, "items": items},

        "item": item,

        "items": items,

    }





@router.post("/search")
async def knowledge_search(body: SearchRequest, db: DbSession, user: CurrentUser) -> dict:
    _ = user
    chunks = await search_chunks(
        db,
        query=body.query,
        top_k=body.topK,
        min_score=body.minScore,
        kb_id=body.baseId,
    )
    return {"code": 0, "msg": "ok", "data": {"chunks": chunks}, "chunks": chunks}





@router.get("/documents/{public_id}/preview")
async def documents_preview(
    public_id: str,
    db: DbSession,
    user: CurrentUser,
    baseId: str = Query(..., min_length=1, max_length=32),
) -> dict:
    _ = user
    data = await get_document_preview(db, base_public_id=baseId, doc_public_id=public_id)
    return ok(data)


@router.delete("/documents/{public_id}")

async def documents_delete(

    public_id: str,

    db: DbSession,

    user: CurrentUser,

    baseId: str = Query(..., min_length=1, max_length=32),

) -> dict:

    _ = user

    base = await bases_svc.get_base_by_public_id(db, baseId)

    result = await db.execute(

        select(KnowledgeDocument).where(

            KnowledgeDocument.public_id == public_id,

            KnowledgeDocument.base_id == base.id,

        )

    )

    doc = result.scalar_one_or_none()

    if doc is None:

        raise AppError(ErrorCode.NOT_FOUND, "document not found", status_code=404)



    deleted = to_kb_item(doc)

    get_qdrant_store().delete_by_doc_id(public_id)

    await db.delete(doc)

    await db.commit()

    items = await list_documents(db, base_public_id=baseId)

    return {

        "code": 0,

        "msg": "ok",

        "data": {"items": items, "deleted": deleted},

        "items": items,

    }


