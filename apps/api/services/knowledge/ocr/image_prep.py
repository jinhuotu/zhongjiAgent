"""把任意位图压成 OCR 可接受的 JPEG。"""

from __future__ import annotations

import io

from common.errors import AppError, ErrorCode

_MAX_EDGE = 4096
_MAX_BYTES = 9_500_000


def to_ocr_jpeg(raw: bytes) -> bytes:
    from PIL import Image

    if not raw:
        raise AppError(ErrorCode.VALIDATION, "OCR 图片为空", status_code=422)
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.BAD_REQUEST, f"无法读取图片：{exc}", status_code=400) from exc

    if max(img.size) > _MAX_EDGE:
        img.thumbnail((_MAX_EDGE, _MAX_EDGE))

    quality = 85
    data = b""
    while quality >= 45:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= _MAX_BYTES:
            return data
        quality -= 10
    return data
