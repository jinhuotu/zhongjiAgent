"""知识库图片：multipart 落盘、后台 OCR、文本向量化。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.knowledge.bases import get_base_by_public_id
from api.services.knowledge.image_contract import (
    ERR_EMBED_FAILED,
    ERR_NO_FILE,
    ERR_OCR_EMPTY,
    ERR_OCR_FAILED,
    ERR_OCR_TIMEOUT,
    ERR_TOO_LARGE,
    ERR_UNSUPPORTED,
    IMAGE_CONTENT_TYPE_EXT,
    IMAGE_EXTENSIONS,
    KIND_IMAGE,
    SUMMARY_EMBED,
    SUMMARY_OCR,
    is_image_ext,
)
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
from api.services.knowledge.ocr.factory import get_ocr_provider
from common.config import get_settings
from common.errors import AppError, ErrorCode
from db.models.knowledge import KnowledgeBase, KnowledgeDocument
from db.session import AsyncSessionLocal

logger = logging.getLogger("api.kb.image")

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


def notify_image_worker(public_id: str) -> None:
    try:
        _queue().put_nowait(public_id)
        _event().set()
    except RuntimeError:
        pass


def _safe_upload_name(raw: str | None) -> str:
    name = Path(raw or "upload").name.strip().replace("\x00", "")
    return name or "upload"


def _sniff_image_ext(filename: str | None, content_type: str | None) -> str:
    name = _safe_upload_name(filename)
    ext = Path(name).suffix.lower().lstrip(".")
    if is_image_ext(ext):
        return "jpg" if ext == "jpeg" else ext
    mime = (content_type or "").split(";")[0].strip().lower()
    mapped = IMAGE_CONTENT_TYPE_EXT.get(mime)
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


async def ocr_image_file(path: Path) -> str:
    provider = get_ocr_provider()

    def _run() -> str:
        return provider.recognize(path.read_bytes())

    text = (await asyncio.to_thread(_run) or "").strip()
    if len(text) < 4:
        raise AppError(ErrorCode.VALIDATION, ERR_OCR_EMPTY, status_code=422)
    return text


async def ingest_image_upload(
    db: AsyncSession,
    *,
    base_public_id: str,
    upload: UploadFile,
    name: str | None = None,
    tags: list[str] | None = None,
    uploader: str | None = None,
    created_by: int | None = None,
) -> dict:
    settings = get_settings()
    ocr_name = (settings.ocr_provider or "none").strip().lower()
    if ocr_name in {"", "none", "off", "false"}:
        raise AppError(
            ErrorCode.VALIDATION,
            "未启用 OCR（OCR_PROVIDER=none）。图片请在 .env 设置 OCR_PROVIDER=rapidocr 或 aliyun",
            status_code=422,
        )
    filename = _safe_upload_name(upload.filename)
    ext = _sniff_image_ext(filename, upload.content_type)
    if ext not in IMAGE_EXTENSIONS and ext != "jpeg":
        raise AppError(ErrorCode.VALIDATION, ERR_UNSUPPORTED, status_code=422)

    ocr_name = (settings.ocr_provider or "none").strip().lower()
    if ocr_name in {"", "none", "off", "false"}:
        raise AppError(
            ErrorCode.VALIDATION,
            "未启用 OCR（OCR_PROVIDER=none）。图片请在 .env 设置 OCR_PROVIDER=rapidocr 或 aliyun",
            status_code=422,
        )
    base = await get_base_by_public_id(db, base_public_id)
    public_id = short_id(12)
    rel_key = f"knowledge/{base_public_id}/{public_id}.{ext}"
    dest = resolve_storage_path(rel_key)
    max_bytes = int(settings.kb_upload_max_bytes)
    size = await _write_upload(upload, dest, max_bytes=max_bytes)

    display_name = (name or "").strip() or Path(filename).stem or filename
    doc = KnowledgeDocument(
        public_id=public_id,
        base_id=base.id,
        name=display_name,
        source="file",
        kind=KIND_IMAGE,
        file_type=ext,
        size=size,
        storage_path=rel_key,
        file_key=rel_key,
        summary=SUMMARY_OCR,
        char_count=0,
        chunk_count=0,
        tags=list(tags) if tags else ["手动上传", "图片"],
        uploader=uploader,
        status="parsing",
        created_by=created_by,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    setattr(doc, "_base_public_id", base.public_id)
    notify_image_worker(public_id)
    return to_kb_item(doc)


def unlink_image_sidecars(base_public_id: str, doc: KnowledgeDocument) -> None:
    unlink_stored_file(doc.file_key or doc.storage_path)
    unlink_stored_file(_extracted_text_key(base_public_id, doc.public_id))


async def process_image_document(public_id: str) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.public_id == public_id)
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            logger.warning("image ingest: doc missing %s", public_id)
            return
        base_row = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == doc.base_id))
        base = base_row.scalar_one_or_none()
        base_pid = base.public_id if base else ""
        try:
            await _process_image_document(db, doc, base_pid)
            await db.commit()
            logger.info("image ingest ready id=%s chunks=%s", public_id, doc.chunk_count)
        except Exception as exc:  # noqa: BLE001
            logger.exception("image ingest failed id=%s", public_id)
            doc.status = "failed"
            doc.error_msg = _error_msg(exc)
            await db.commit()


async def _process_image_document(
    db: AsyncSession,
    doc: KnowledgeDocument,
    base_pid: str,
) -> None:
    doc.kind = KIND_IMAGE
    doc.summary = SUMMARY_OCR
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
            text = await ocr_image_file(path)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppError(ErrorCode.INTERNAL, ERR_OCR_FAILED, status_code=500) from exc
        if len(text) < 4:
            raise AppError(ErrorCode.VALIDATION, ERR_OCR_EMPTY, status_code=422)
        if base_pid:
            write_extracted_text(base_pid, doc.public_id, text)

    heading = (doc.name or "").strip()
    if heading and heading.lower() not in text.lower():
        text = f"{heading}\n{text}"

    logger.info(
        "kb image ocr done id=%s ms=%s chars=%s",
        doc.public_id,
        int((time.monotonic() - t0) * 1000),
        len(text),
    )

    doc.summary = SUMMARY_EMBED
    await db.commit()
    await db.refresh(doc)

    t1 = time.monotonic()
    doc.char_count = len(text)
    doc.chunk_count = 0
    doc.status = "ready"
    doc.error_msg = None
    try:
        await _vectorize_document(db, doc, text)
    except AppError as exc:
        if "too short" in (exc.msg or "").lower():
            raise AppError(ErrorCode.VALIDATION, ERR_OCR_EMPTY, status_code=422) from exc
        raise AppError(ErrorCode.INTERNAL, ERR_EMBED_FAILED, status_code=500) from exc
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.INTERNAL, ERR_EMBED_FAILED, status_code=500) from exc
    doc.summary = text[:200]
    logger.info(
        "kb image embed done id=%s ms=%s chunks=%s",
        doc.public_id,
        int((time.monotonic() - t1) * 1000),
        doc.chunk_count,
    )


async def reclaim_parsing_images() -> int:
    n = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.status == "parsing",
                KnowledgeDocument.kind == KIND_IMAGE,
            )
        )
        docs = list(result.scalars().all())
        for doc in docs:
            notify_image_worker(doc.public_id)
            n += 1
    if n:
        logger.info("requeued parsing images count=%s", n)
    return n


async def _run_claimed(public_id: str) -> None:
    timeout = max(60, int(get_settings().ocr_timeout_seconds) * 20)
    try:
        await asyncio.wait_for(process_image_document(public_id), timeout=timeout)
    except TimeoutError:
        logger.warning("image ingest timeout id=%s", public_id)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.public_id == public_id)
            )
            doc = result.scalar_one_or_none()
            if doc is not None and doc.status == "parsing":
                doc.status = "failed"
                doc.error_msg = ERR_OCR_TIMEOUT
                await db.commit()


async def run_image_worker() -> None:
    logger.info("kb image worker started")
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
            logger.info("kb image worker cancelled")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("kb image worker loop error")
            await asyncio.sleep(1.0)


def start_image_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(run_image_worker(), name="kb-image-worker")
