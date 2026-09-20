"""视频 ASR：抽音轨 → 转写 → 旁路正文/字幕 → 供向量化。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from common.config import get_settings
from common.errors import AppError, ErrorCode

from api.services.knowledge.asr.audio_prep import prepare_asr_media
from api.services.knowledge.asr.base import TranscriptResult, TranscriptSegment
from api.services.knowledge.asr.chunking import (
    TimedChunk,
    split_transcript_segments,
    timed_chunks_from_text,
)
from api.services.knowledge.asr.factory import get_asr_provider
from api.services.knowledge.video_contract import ERR_ASR_EMPTY, ERR_ASR_FAILED

logger = logging.getLogger("api.kb.asr")


def _storage_root() -> Path:
    return Path(get_settings().storage_root).expanduser().resolve()


def _resolve_key(key: str) -> Path:
    rel = (key or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise AppError(ErrorCode.BAD_REQUEST, "invalid storage key", status_code=400)
    root = _storage_root()
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise AppError(ErrorCode.BAD_REQUEST, "invalid storage key", status_code=400)
    return path


def _cues_key(base_public_id: str, doc_public_id: str) -> str:
    return f"knowledge/{base_public_id}/{doc_public_id}.asr.json"


def write_asr_cues(
    base_public_id: str, doc_public_id: str, segments: list[TranscriptSegment]
) -> None:
    path = _resolve_key(_cues_key(base_public_id, doc_public_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "segments": [
            {"text": s.text, "startMs": int(s.start_ms), "endMs": int(s.end_ms)}
            for s in segments
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_asr_cues(base_public_id: str, doc_public_id: str) -> list[TranscriptSegment]:
    try:
        path = _resolve_key(_cues_key(base_public_id, doc_public_id))
    except AppError:
        return []
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rows = raw.get("segments") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[TranscriptSegment] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start_ms = int(item.get("startMs") or 0)
            end_ms = int(item.get("endMs") or start_ms)
        except (TypeError, ValueError):
            start_ms, end_ms = 0, 0
        out.append(TranscriptSegment(text=text, start_ms=start_ms, end_ms=end_ms))
    return out


def unlink_asr_cues(base_public_id: str, doc_public_id: str) -> None:
    try:
        path = _resolve_key(_cues_key(base_public_id, doc_public_id))
    except AppError:
        return
    if path.is_file():
        path.unlink(missing_ok=True)


def build_timed_chunks(
    text: str, *, base_public_id: str = "", doc_public_id: str = ""
) -> list[TimedChunk]:
    segs = read_asr_cues(base_public_id, doc_public_id) if base_public_id and doc_public_id else []
    if segs:
        chunks = split_transcript_segments(segs)
        if chunks:
            return chunks
    return timed_chunks_from_text(text)


async def transcribe_video_file(
    video_path: Path, *, display_name: str | None = None
) -> TranscriptResult:
    import asyncio

    provider = get_asr_provider()

    def _run() -> TranscriptResult:
        media, is_tmp = prepare_asr_media(video_path)
        try:
            return provider.transcribe(media, filename=display_name or media.name)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("asr failed path=%s", video_path)
            detail = str(exc).strip()
            msg = f"{ERR_ASR_FAILED}：{detail}" if detail else ERR_ASR_FAILED
            raise AppError(ErrorCode.VALIDATION, msg[:500], status_code=422) from exc
        finally:
            if is_tmp:
                Path(media).unlink(missing_ok=True)

    result = await asyncio.to_thread(_run)
    if not (result.text or "").strip():
        raise AppError(ErrorCode.VALIDATION, ERR_ASR_EMPTY, status_code=422)
    return result
