"""按 ASR_PROVIDER 创建转写器。none = 未启用。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Protocol

from common.config import get_settings
from common.errors import AppError, ErrorCode

from api.services.knowledge.asr.base import TranscriptResult


class AsrProvider(Protocol):
    def transcribe(self, media_path: Path, *, filename: str | None = None) -> TranscriptResult:
        ...


class DisabledAsrProvider:
    def transcribe(self, media_path: Path, *, filename: str | None = None) -> TranscriptResult:
        _ = media_path, filename
        raise AppError(
            ErrorCode.VALIDATION,
            "未启用语音转写（ASR_PROVIDER=none）。请在 .env 设置 ASR_PROVIDER=openai 并配置 ASR_API_BASE",
            status_code=422,
        )


@lru_cache
def get_asr_provider() -> AsrProvider:
    settings = get_settings()
    name = (settings.asr_provider or "none").strip().lower()
    if name in {"", "none", "off", "false"}:
        return DisabledAsrProvider()
    if name in {"openai", "whisper", "http"}:
        from api.services.knowledge.asr.openai_compat import OpenAiCompatAsrProvider

        return OpenAiCompatAsrProvider(
            api_base=settings.asr_api_base,
            api_key=settings.asr_api_key,
            model=settings.asr_model,
            language=settings.asr_language,
            timeout_seconds=int(settings.kb_video_asr_timeout_seconds),
        )
    raise AppError(
        ErrorCode.VALIDATION,
        f"不支持的 ASR_PROVIDER={name}（可用 none / openai）",
        status_code=422,
    )
