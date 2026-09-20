"""RapidOCR 本地识别（onnxruntime）。无需云密钥。"""

from __future__ import annotations

import logging
from typing import Any

from common.errors import AppError, ErrorCode

from api.services.knowledge.ocr.image_prep import to_ocr_jpeg

logger = logging.getLogger("api.kb.ocr")


def _load_rapid_ocr():
    try:
        from rapidocr import RapidOCR

        return RapidOCR()
    except ImportError:
        pass
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR()
    except ImportError as exc:
        raise AppError(
            ErrorCode.VALIDATION,
            "未安装 RapidOCR。请 poetry add rapidocr pillow 后设置 OCR_PROVIDER=rapidocr",
            status_code=422,
        ) from exc


def _texts_from_result(raw: Any) -> list[str]:
    result = raw[0] if isinstance(raw, tuple) and raw else raw
    txts = getattr(result, "txts", None)
    if txts:
        return [str(t).strip() for t in txts if str(t).strip()]
    rec = getattr(result, "rec_res", None) or getattr(result, "rec_texts", None)
    if rec:
        return [str(t).strip() for t in rec if str(t).strip()]
    lines: list[str] = []
    for row in result or []:
        if isinstance(row, str) and row.strip():
            lines.append(row.strip())
            continue
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            text = str(row[1] or "").strip()
            if text:
                lines.append(text)
    return lines


class RapidOcrProvider:
    def __init__(self) -> None:
        self._engine = _load_rapid_ocr()

    def recognize(self, image: bytes) -> str:
        jpeg = to_ocr_jpeg(image)
        try:
            raw = self._engine(jpeg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rapidocr failed: %s", exc)
            raise AppError(ErrorCode.INTERNAL, f"RapidOCR 失败：{exc}", status_code=502) from exc
        out = "\n".join(_texts_from_result(raw)).strip()
        logger.info("rapidocr ok chars=%s", len(out))
        return out
