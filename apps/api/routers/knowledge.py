from __future__ import annotations



import re



import httpx

from fastapi import APIRouter, File, Form, Query, UploadFile

from sqlalchemy import select



from api.deps import CurrentUser, DbSession
from api.services.menus import user_is_admin

from api.schemas.knowledge import (
    CreateBaseRequest,
    ReplaceAclRequest,
    SearchRequest,
    TextDocumentRequest,
    UpdateBaseRequest,
    UploadDocumentRequest,
    UrlDocumentRequest,
)
from api.services.knowledge import access as kb_access
from api.services.knowledge import bases as bases_svc

from api.services.knowledge.ingest import (
    download_document,
    get_document_preview,
    ingest_text,
    list_documents,
    resolve_document_file,
    search_chunks,
    to_kb_item,
)
from api.services.knowledge.image import ingest_image_upload, unlink_image_sidecars
from api.services.knowledge.video import ingest_video_upload, unlink_video_sidecars

from api.services.knowledge.qdrant_store import get_qdrant_store

from common.errors import AppError, ErrorCode

from common.response import ok

from db.models.knowledge import KnowledgeDocument

from urllib.parse import quote

from fastapi.responses import FileResponse, Response



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
async def bases_list(
    db: DbSession,
    user: CurrentUser,
    access: str = Query(default="view"),
) -> dict:
    perm: kb_access.Perm = "view"
    raw = (access or "view").strip().lower()
    if raw in {kb_access.PERM_VIEW, kb_access.PERM_USE, kb_access.PERM_MANAGE}:
        perm = raw  # type: ignore[assignment]
    items, can_create = await kb_access.list_visible_bases(db, user, access=perm)
    return ok({"items": items, "canCreate": can_create})


@router.post("/bases")
async def bases_create(body: CreateBaseRequest, db: DbSession, user: CurrentUser) -> dict:
    if not user_is_admin(user):
        raise AppError(ErrorCode.FORBIDDEN, "仅管理员可创建知识库", status_code=403)
    item = await bases_svc.create_base(
        db,
        name=body.name,
        description=body.description,
        created_by=user.id,
    )
    return ok({"item": kb_access.attach_perms(item, set(kb_access.ALL_PERMS))})


@router.get("/bases/{base_id}")
async def bases_get(base_id: str, db: DbSession, user: CurrentUser) -> dict:
    base = await kb_access.require_base(db, user, base_id, kb_access.PERM_VIEW)
    flags = await kb_access.perms_for_base(db, user, base)
    return ok({"item": kb_access.attach_perms(bases_svc.to_base_item(base), flags)})


@router.get("/bases/{base_id}/acl")
async def bases_acl_get(base_id: str, db: DbSession, user: CurrentUser) -> dict:
    return ok(await kb_access.list_acl(db, user, base_id))


@router.put("/bases/{base_id}/acl")
async def bases_acl_put(
    base_id: str, body: ReplaceAclRequest, db: DbSession, user: CurrentUser
) -> dict:
    data = await kb_access.replace_acl(
        db,
        user,
        base_id,
        [g.model_dump() for g in body.grants],
    )
    return ok(data)


@router.patch("/bases/{base_id}")
async def bases_update(
    base_id: str,
    body: UpdateBaseRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    await kb_access.require_base(db, user, base_id, kb_access.PERM_MANAGE)
    item = await bases_svc.update_base(
        db,
        public_id=base_id,
        name=body.name,
        description=body.description,
    )
    base = await bases_svc.get_base_by_public_id(db, base_id)
    flags = await kb_access.perms_for_base(db, user, base)
    return ok({"item": kb_access.attach_perms(item, flags)})


@router.delete("/bases/{base_id}")
async def bases_delete(base_id: str, db: DbSession, user: CurrentUser) -> dict:
    await kb_access.require_base(db, user, base_id, kb_access.PERM_MANAGE)

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

    await kb_access.require_base(db, user, baseId, kb_access.PERM_VIEW)

    items = await list_documents(db, base_public_id=baseId)

    return {"code": 0, "msg": "ok", "data": {"items": items}, "items": items}





@router.post("/documents/upload")

async def documents_upload(

    body: UploadDocumentRequest,

    db: DbSession,

    user: CurrentUser,

) -> dict:

    await kb_access.require_base(db, user, body.baseId, kb_access.PERM_MANAGE)

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

    await kb_access.require_base(db, user, body.baseId, kb_access.PERM_MANAGE)

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

    await kb_access.require_base(db, user, body.baseId, kb_access.PERM_MANAGE)

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
    if body.baseId:
        await kb_access.require_base(db, user, body.baseId, kb_access.PERM_USE)
        kb_ids = [body.baseId]
    else:
        kb_ids = await kb_access.usable_public_ids(db, user)
        if not kb_ids:
            return {"code": 0, "msg": "ok", "data": {"chunks": []}, "chunks": []}
    chunks = await search_chunks(
        db,
        query=body.query,
        top_k=body.topK,
        min_score=body.minScore,
        kb_ids=kb_ids,
    )
    return {"code": 0, "msg": "ok", "data": {"chunks": chunks}, "chunks": chunks}





@router.get("/documents/{public_id}/preview")
async def documents_preview(
    public_id: str,
    db: DbSession,
    user: CurrentUser,
    baseId: str = Query(..., min_length=1, max_length=32),
) -> dict:
    await kb_access.require_base(db, user, baseId, kb_access.PERM_VIEW)
    data = await get_document_preview(db, base_public_id=baseId, doc_public_id=public_id)
    return ok(data)


@router.get("/documents/{public_id}/download")
async def documents_download(
    public_id: str,
    db: DbSession,
    user: CurrentUser,
    baseId: str = Query(..., min_length=1, max_length=32),
) -> Response:
    await kb_access.require_base(db, user, baseId, kb_access.PERM_VIEW)
    data, filename, media_type = await download_document(
        db, base_public_id=baseId, doc_public_id=public_id
    )
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            # 便于前端 blob 预览图片时跨域读取
            "Cache-Control": "private, max-age=60",
        },
    )


@router.post("/documents/upload-video")
async def documents_upload_video(
    db: DbSession,
    user: CurrentUser,
    file: UploadFile = File(..., description="视频原片：mp4 / webm"),
    baseId: str = Form(..., min_length=1, max_length=32),
    name: str | None = Form(default=None),
    tags: str | None = Form(default=None),
) -> dict:
    await kb_access.require_base(db, user, baseId, kb_access.PERM_MANAGE)
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or ["手动上传", "视频"]
    item = await ingest_video_upload(
        db,
        base_public_id=baseId,
        upload=file,
        name=name,
        tags=tag_list,
        uploader=user.display_name or user.username,
        created_by=user.id,
    )
    items = await list_documents(db, base_public_id=baseId)
    return {
        "code": 0,
        "msg": "ok",
        "data": {"item": item, "items": items},
        "item": item,
        "items": items,
    }


@router.post("/documents/upload-image")
async def documents_upload_image(
    db: DbSession,
    user: CurrentUser,
    file: UploadFile = File(..., description="图片原件：png / jpg / jpeg / webp / gif / bmp"),
    baseId: str = Form(..., min_length=1, max_length=32),
    name: str | None = Form(default=None),
    tags: str | None = Form(default=None),
) -> dict:
    await kb_access.require_base(db, user, baseId, kb_access.PERM_MANAGE)
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or ["手动上传", "图片"]
    item = await ingest_image_upload(
        db,
        base_public_id=baseId,
        upload=file,
        name=name,
        tags=tag_list,
        uploader=user.display_name or user.username,
        created_by=user.id,
    )
    items = await list_documents(db, base_public_id=baseId)
    return {
        "code": 0,
        "msg": "ok",
        "data": {"item": item, "items": items},
        "item": item,
        "items": items,
    }


@router.get("/documents/{public_id}/file")
async def documents_file(
    public_id: str,
    db: DbSession,
    user: CurrentUser,
    baseId: str = Query(..., min_length=1, max_length=32),
) -> FileResponse:
    """原件流式播放/预览：支持 Range，供 <video src> / <img src> 使用。"""
    await kb_access.require_base(db, user, baseId, kb_access.PERM_VIEW)
    path, filename, media_type = await resolve_document_file(
        db, base_public_id=baseId, doc_public_id=public_id
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=60",
        },
    )


@router.delete("/documents/{public_id}")

async def documents_delete(

    public_id: str,

    db: DbSession,

    user: CurrentUser,

    baseId: str = Query(..., min_length=1, max_length=32),

) -> dict:

    await kb_access.require_base(db, user, baseId, kb_access.PERM_MANAGE)

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

    unlink_video_sidecars(base.public_id, doc)
    unlink_image_sidecars(base.public_id, doc)

    await db.delete(doc)

    await db.commit()

    items = await list_documents(db, base_public_id=baseId)

    return {

        "code": 0,

        "msg": "ok",

        "data": {"items": items, "deleted": deleted},

        "items": items,

    }


