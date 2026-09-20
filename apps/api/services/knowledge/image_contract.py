"""知识库图片契约：OCR 文本检索 + 命中后展示原图。"""

from __future__ import annotations

IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "gif", "bmp"})

IMAGE_CONTENT_TYPE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
}

IMAGE_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}

KIND_IMAGE = "image"
PREVIEW_KIND_IMAGE = "image"

SUMMARY_OCR = "正在识别文字…"
SUMMARY_EMBED = "正在写入检索…"
SUMMARY_PENDING = "图片原件（待识别，暂不可检索）"

ERR_UNSUPPORTED = "不支持的图片格式"
ERR_TOO_LARGE = "图片超过大小上限"
ERR_OCR_FAILED = "文字识别失败"
ERR_OCR_EMPTY = "未识别到有效文字"
ERR_EMBED_FAILED = "向量化失败"
ERR_NO_FILE = "图片原件缺失，无法识别"
ERR_OCR_TIMEOUT = "文字识别超时，请稍后重试"


def is_image_ext(ext: str | None) -> bool:
    return (ext or "").lower().lstrip(".") in IMAGE_EXTENSIONS
