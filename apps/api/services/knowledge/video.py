"""知识库视频：multipart 落盘、后台 ASR 转写、带时间戳向量化。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.knowledge.asr.factory import get_asr_provider
from api.services.knowledge.asr.pipeline import (
    build_timed_chunks,
    transcribe_video_file,
    unlink_asr_cues,
    write_asr_cues,
)
from api.services.knowledge.bases import get_base_by_public_id
from api.services.knowledge.ingest import (
    _error_msg,
    _extracted_text_key,
    _vectorize_document,
    read_extracted_text,
    resolve_storage_path,
    short_id,
    to_kb_item,
    unlink_stored_file,
    write_extracted_text,
)
from api.services.knowledge.video_contract import (
    ERR_ASR_EMPTY,
    ERR_ASR_FAILED,
    ERR_ASR_TIMEOUT,
    ERR_EMBED_FAILED,
    ERR_NO_FILE,
    ERR_TOO_LARGE,
    ERR_UNSUPPORTED,
    KIND_VIDEO,
    SUMMARY_ASR,
    SUMMARY_EMBED,
    VIDEO_CONTENT_TYPE_EXT,
    VIDEO_EXTENSIONS,
    is_video_ext,
)
from common.config import get_settings
from common.errors import AppError, ErrorCode
from db.models.knowledge import KnowledgeBase, KnowledgeDocument
from db.session import AsyncSessionLocal

logger = logging.getLogger("api.kb.video")

_wakeup: asyncio.Event | None = None
_worker_task: asyncio.Task | None = None
_pending: asyncio.Queue[str] | None = None


def _event() -> asyncio.Event:
    global _wakeup
    if _wakeup is None:
        _wakeup = asyncio.Event()
    return _wakeup


def _queue() -> asyncio.Queue[str]:
    global _pending
    if _pending is None:
        _pending = asyncio.Queue()
    return _pending


def notify_video_worker(public_id: str) -> None:
    try:
        _queue().put_nowait(public_id)
        _event().set()
    except RuntimeError:
        pass


def _safe_upload_name(raw: str | None) -> str:
    name = Path(raw or "upload").name.strip().replace("\x00", "")
    return name or "upload"


def _sniff_video_ext(filename: str | None, content_type: str | None) -> str:
    name = _safe_upload_name(filename)
    ext = Path(name).suffix.lower().lstrip(".")
    if is_video_ext(ext):
        return ext
    mime = (content_type or "").split(";")[0].strip().lower()
    mapped = VIDEO_CONTENT_TYPE_EXT.get(mime)
    if mapped:
        return mapped
    raise AppError(ErrorCode.VALIDATION, ERR_UNSUPPORTED, status_code=422)


async def _write_upload(upload: UploadFile, dest: Path, *, max_bytes: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise AppError(
                        ErrorCode.VALIDATION,
                        f"{ERR_TOO_LARGE}（{max_bytes} 字节）",
                        status_code=422,
                    )
                fh.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if size <= 0:
        dest.unlink(missing_ok=True)
        raise AppError(ErrorCode.VALIDATION, "空文件无法入库", status_code=422)
    return size


async def ingest_video_upload(
    db: AsyncSession,
    *,
    base_public_id: str,
    upload: UploadFile,
    name: str | None = None,
    tags: list[str] | None = None,
    uploader: str | None = None,
    created_by: int | None = None,
) -> dict:
    """落盘 mp4/webm，排队语音转写后写入同一套知识库检索。"""
    settings = get_settings()
    filename = _safe_upload_name(upload.filename)
    ext = _sniff_video_ext(filename, upload.content_type)
    if ext not in VIDEO_EXTENSIONS:
        raise AppError(ErrorCode.VALIDATION, ERR_UNSUPPORTED, status_code=422)

    get_asr_provider()
    base = await get_base_by_public_id(db, base_public_id)
    public_id = short_id(12)
    rel_key = f"knowledge/{base_public_id}/{public_id}.{ext}"
    dest = resolve_storage_path(rel_key)
    max_bytes = int(settings.kb_video_upload_max_bytes)
    size = await _write_upload(upload, dest, max_bytes=max_bytes)

    display_name = (name or "").strip() or Path(filename).stem or filename
    doc = KnowledgeDocument(
        public_id=public_id,
        base_id=base.id,
        name=display_name,
        source="file",
        kind=KIND_VIDEO,
        file_type=ext,
        size=size,
        storage_path=rel_key,
        file_key=rel_key,
        summary=SUMMARY_ASR,
        char_count=0,
        chunk_count=0,
        tags=list(tags) if tags else ["手动上传", "视频"],
        uploader=uploader,
        status="parsing",
        created_by=created_by,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    setattr(doc, "_base_public_id", base.public_id)
    notify_video_worker(public_id)
    return to_kb_item(doc)


def unlink_video_sidecars(base_public_id: str, doc: KnowledgeDocument) -> None:
    unlink_stored_file(doc.file_key or doc.storage_path)
    unlink_stored_file(_extracted_text_key(base_public_id, doc.public_id))
    unlink_asr_cues(base_public_id, doc.public_id)


async def process_video_document(public_id: str) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.public_id == public_id)
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            logger.warning("video ingest: doc missing %s", public_id)
            return
        base_row = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == doc.base_id))
        base = base_row.scalar_one_or_none()
        base_pid = base.public_id if base else ""
        try:
            await _process_video_document(db, doc, base_pid)
            await db.commit()
            logger.info("video ingest ready id=%s chunks=%s", public_id, doc.chunk_count)
        except Exception as exc:  # noqa: BLE001
            logger.exception("video ingest failed id=%s", public_id)
            doc.status = "failed"
            doc.error_msg = _error_msg(exc)
            await db.commit()


async def _process_video_document(
    db: AsyncSession,
    doc: KnowledgeDocument,
    base_pid: str,
) -> None:
    doc.kind = KIND_VIDEO
    doc.summary = SUMMARY_ASR
    doc.error_msg = None
    await db.commit()
    await db.refresh(doc)

    t0 = time.monotonic()
    text = ""
    if base_pid:
        text = (read_extracted_text(base_pid, doc.public_id) or "").strip()

    if len(text) < 4:
        key = doc.file_key or doc.storage_path
        if not key:
            raise AppError(ErrorCode.VALIDATION, ERR_NO_FILE, status_code=422)
        path = resolve_storage_path(key)
        if not path.is_file():
            raise AppError(ErrorCode.VALIDATION, ERR_NO_FILE, status_code=404)
        try:
            result = await transcribe_video_file(
                path,
                display_name=f"{doc.name or 'video'}.{doc.file_type or 'mp4'}",
            )
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppError(ErrorCode.INTERNAL, ERR_ASR_FAILED, status_code=500) from exc
        text = (result.text or "").strip()
        if len(text) < 4:
            raise AppError(ErrorCode.VALIDATION, ERR_ASR_EMPTY, status_code=422)
        if base_pid:
            write_extracted_text(base_pid, doc.public_id, text)
            write_asr_cues(base_pid, doc.public_id, result.segments)

    logger.info(
        "kb video asr done id=%s ms=%s chars=%s",
        doc.public_id,
        int((time.monotonic() - t0) * 1000),
        len(text),
    )

    doc.summary = SUMMARY_EMBED
    await db.commit()
    await db.refresh(doc)

    t1 = time.monotonic()
    timed = build_timed_chunks(text, base_public_id=base_pid, doc_public_id=doc.public_id)
    doc.char_count = len(text)
    doc.chunk_count = 0
    doc.status = "ready"
    doc.error_msg = None
    try:
        await _vectorize_document(db, doc, text, timed_chunks=timed)
    except AppError as exc:
        if "too short" in (exc.msg or "").lower():
            raise AppError(ErrorCode.VALIDATION, ERR_ASR_EMPTY, status_code=422) from exc
        raise AppError(ErrorCode.INTERNAL, ERR_EMBED_FAILED, status_code=500) from exc
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.INTERNAL, ERR_EMBED_FAILED, status_code=500) from exc
    doc.summary = text[:200]
    logger.info(
        "kb video embed done id=%s ms=%s chunks=%s",
        doc.public_id,
        int((time.monotonic() - t1) * 1000),
        doc.chunk_count,
    )


async def reclaim_parsing_videos() -> int:
    """启动时把仍在 parsing 的视频重新入队。"""
    n = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.status == "parsing",
                KnowledgeDocument.kind == KIND_VIDEO,
            )
        )
        docs = list(result.scalars().all())
        for doc in docs:
            notify_video_worker(doc.public_id)
            n += 1
    if n:
        logger.info("requeued parsing videos count=%s", n)
    return n


async def _run_claimed(public_id: str) -> None:
    timeout = max(60, int(get_settings().kb_video_asr_timeout_seconds))
    try:
        await asyncio.wait_for(process_video_document(public_id), timeout=timeout)
    except TimeoutError:
        logger.warning("video ingest timeout id=%s", public_id)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.public_id == public_id)
            )
            doc = result.scalar_one_or_none()
            if doc is not None and doc.status == "parsing":
                doc.status = "failed"
                doc.error_msg = ERR_ASR_TIMEOUT
                await db.commit()


async def run_video_worker() -> None:
    logger.info("kb video worker started")
    ev = _event()
    q = _queue()
    while True:
        try:
            try:
                public_id = q.get_nowait()
            except asyncio.QueueEmpty:
                ev.clear()
                try:
                    await asyncio.wait_for(ev.wait(), timeout=2.0)
                except TimeoutError:
                    continue
                continue
            await _run_claimed(public_id)
        except asyncio.CancelledError:
            logger.info("kb video worker cancelled")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("kb video worker loop error")
            await asyncio.sleep(1.0)


def start_video_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(run_video_worker(), name="kb-video-worker")
