"""按 OCR_PROVIDER 创建识别器。none 表示未启用。"""

from __future__ import annotations

from functools import lru_cache

from common.config import get_settings
from common.errors import AppError, ErrorCode

from api.services.knowledge.ocr.base import OcrProvider


class DisabledOcrProvider:
    def recognize(self, image: bytes) -> str:
        _ = image
        raise AppError(
            ErrorCode.VALIDATION,
            "未启用 OCR（OCR_PROVIDER=none）。图片请在 .env 设置 OCR_PROVIDER=rapidocr 或 aliyun",
            status_code=422,
        )


@lru_cache
def get_ocr_provider() -> OcrProvider:
    settings = get_settings()
    name = (settings.ocr_provider or "none").strip().lower()
    if name in {"", "none", "off", "false"}:
        return DisabledOcrProvider()
    if name in {"rapidocr", "rapid"}:
        from api.services.knowledge.ocr.rapidocr import RapidOcrProvider

        return RapidOcrProvider()
    if name == "aliyun":
        from api.services.knowledge.ocr.aliyun import AliyunOcrProvider

        return AliyunOcrProvider(
            access_key_id=settings.ocr_access_key_id,
            access_key_secret=settings.ocr_access_key_secret,
            endpoint=settings.ocr_endpoint,
            ocr_type=settings.ocr_type,
            timeout_seconds=int(settings.ocr_timeout_seconds),
        )
    raise AppError(
        ErrorCode.VALIDATION,
        f"不支持的 OCR_PROVIDER={name}（可用 none / aliyun / rapidocr）",
        status_code=422,
    )
