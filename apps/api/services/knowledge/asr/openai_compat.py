"""OpenAI 兼容 /v1/audio/transcriptions（含本地 Whisper HTTP 服务）。"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from common.errors import AppError, ErrorCode

from api.services.knowledge.asr.base import TranscriptResult, TranscriptSegment
from api.services.knowledge.video_contract import ERR_ASR_EMPTY, ERR_ASR_FAILED, ERR_ASR_TIMEOUT

logger = logging.getLogger("api.kb.asr.openai")


class OpenAiCompatAsrProvider:
    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        language: str,
        timeout_seconds: int,
    ) -> None:
        base = (api_base or "").strip().rstrip("/")
        if not base:
            raise AppError(
                ErrorCode.VALIDATION,
                "未配置 ASR_API_BASE（OpenAI 兼容语音转写地址）",
                status_code=422,
            )
        self._base = base
        self._key = (api_key or "").strip()
        self._model = (model or "whisper-1").strip() or "whisper-1"
        self._language = (language or "").strip()
        self._timeout = max(30, int(timeout_seconds or 600))

    def transcribe(self, media_path: Path, *, filename: str | None = None) -> TranscriptResult:
        url = f"{self._base}/audio/transcriptions"
        name = filename or media_path.name
        mime = "audio/mpeg" if media_path.suffix.lower() == ".mp3" else "application/octet-stream"
        headers: dict[str, str] = {}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"

        data: dict[str, str] = {
            "model": self._model,
            "response_format": "verbose_json",
        }
        if self._language:
            data["language"] = self._language

        try:
            with media_path.open("rb") as fh:
                files = {"file": (name, fh, mime)}
                payload = {**data, "timestamp_granularities[]": "segment"}
                with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                    resp = client.post(url, headers=headers, data=payload, files=files)
        except httpx.TimeoutException as exc:
            raise AppError(ErrorCode.VALIDATION, ERR_ASR_TIMEOUT, status_code=422) from exc
        except httpx.HTTPError as ext:
            raise AppError(
                ErrorCode.VALIDATION,
                f"{ERR_ASR_FAILED}：{ext}",
                status_code=422,
            ) from ext

        if resp.status_code >= 400:
            if resp.status_code in {400, 422} and "timestamp" in (resp.text or "").lower():
                return self._transcribe_plain(url, headers, media_path, name, mime, data)
            detail = (resp.text or "")[:300].strip()
            raise AppError(
                ErrorCode.VALIDATION,
                f"{ERR_ASR_FAILED}（HTTP {resp.status_code}）{('：' + detail) if detail else ''}",
                status_code=422,
            )

        return self._parse_body(resp)

    def _transcribe_plain(
        self,
        url: str,
        headers: dict[str, str],
        media_path: Path,
        name: str,
        mime: str,
        data: dict[str, str],
    ) -> TranscriptResult:
        try:
            with media_path.open("rb") as fh:
                files = {"file": (name, fh, mime)}
                with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                    resp = client.post(url, headers=headers, data=data, files=files)
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.VALIDATION,
                f"{ERR_ASR_FAILED}：{exc}",
                status_code=422,
            ) from exc
        if resp.status_code >= 400:
            detail = (resp.text or "")[:300].strip()
            raise AppError(
                ErrorCode.VALIDATION,
                f"{ERR_ASR_FAILED}（HTTP {resp.status_code}）{('：' + detail) if detail else ''}",
                status_code=422,
            )
        return self._parse_body(resp)

    def _parse_body(self, resp: httpx.Response) -> TranscriptResult:
        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/json" not in ctype and not (resp.text or "").lstrip().startswith("{"):
            text = (resp.text or "").strip()
            if not text:
                raise AppError(ErrorCode.VALIDATION, ERR_ASR_EMPTY, status_code=422)
            return TranscriptResult(text=text, segments=[])

        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise AppError(ErrorCode.VALIDATION, "语音转写返回无法解析", status_code=422) from exc

        if isinstance(payload, str):
            text = payload.strip()
            if not text:
                raise AppError(ErrorCode.VALIDATION, ERR_ASR_EMPTY, status_code=422)
            return TranscriptResult(text=text, segments=[])

        text = str(payload.get("text") or "").strip()
        segs_raw = payload.get("segments") or []
        segments: list[TranscriptSegment] = []
        if isinstance(segs_raw, list):
            for item in segs_raw:
                if not isinstance(item, dict):
                    continue
                piece = str(item.get("text") or "").strip()
                if not piece:
                    continue
                try:
                    start_s = float(item.get("start") or 0.0)
                    end_s = float(item.get("end") or start_s)
                except (TypeError, ValueError):
                    start_s, end_s = 0.0, 0.0
                segments.append(
                    TranscriptSegment(
                        text=piece,
                        start_ms=max(0, int(start_s * 1000)),
                        end_ms=max(0, int(end_s * 1000)),
                    )
                )
        if not text and segments:
            text = "".join(s.text for s in segments).strip()
        if not text:
            raise AppError(ErrorCode.VALIDATION, ERR_ASR_EMPTY, status_code=422)
        lang = payload.get("language")
        return TranscriptResult(
            text=text,
            segments=segments,
            language=str(lang).strip() if lang else None,
        )
