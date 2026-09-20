"""转写片段切块：按字数合并 segment，保留 startMs/endMs。"""

from __future__ import annotations

from dataclasses import dataclass

from common.config import get_settings

from api.services.knowledge.asr.base import TranscriptSegment


@dataclass
class TimedChunk:
    content: str
    start_ms: int | None = None
    end_ms: int | None = None


def split_transcript_segments(segments: list[TranscriptSegment]) -> list[TimedChunk]:
    settings = get_settings()
    chunk_size = max(120, int(settings.kb_chunk_size or 600))
    cleaned = [s for s in segments if (s.text or "").strip()]
    if not cleaned:
        return []

    out: list[TimedChunk] = []
    parts: list[str] = []
    start_ms: int | None = None
    end_ms: int | None = None

    def flush() -> None:
        nonlocal parts, start_ms, end_ms
        body = "".join(parts).strip()
        if body:
            out.append(TimedChunk(content=body, start_ms=start_ms, end_ms=end_ms))
        parts = []
        start_ms = None
        end_ms = None

    for seg in cleaned:
        piece = seg.text.strip()
        if not piece:
            continue
        tentative = piece if not parts else f"{''.join(parts)}{piece}"
        if parts and len(tentative) > chunk_size:
            flush()
        if not parts:
            start_ms = int(seg.start_ms)
        parts.append(piece)
        end_ms = int(seg.end_ms)

    flush()
    return out


def timed_chunks_from_text(text: str) -> list[TimedChunk]:
    """无时间戳时退回普通切块。"""
    from api.services.knowledge.chunking import split_text

    return [TimedChunk(content=c, start_ms=None, end_ms=None) for c in split_text(text)]
