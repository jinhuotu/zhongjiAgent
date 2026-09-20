"""语音转写结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TranscriptSegment:
    text: str
    start_ms: int
    end_ms: int


@dataclass
class TranscriptResult:
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None
