"""知识库视频契约常量。一期：ASR 转写文本检索 + 命中后播原片。"""

from __future__ import annotations

VIDEO_EXTENSIONS = frozenset({"mp4", "webm"})

VIDEO_CONTENT_TYPE_EXT = {
    "video/mp4": "mp4",
    "video/webm": "webm",
}

VIDEO_EXT_MIME = {
    "mp4": "video/mp4",
    "webm": "video/webm",
}

KIND_VIDEO = "video"
PREVIEW_KIND_VIDEO = "video"

SUMMARY_ASR = "正在语音转写…"
SUMMARY_EMBED = "正在写入检索…"
SUMMARY_PENDING_ASR = "视频原片（待转写，暂不可检索）"

ERR_UNSUPPORTED = "不支持的视频格式"
ERR_TOO_LARGE = "视频超过大小上限"
ERR_ASR_FAILED = "语音转写失败"
ERR_ASR_EMPTY = "未识别到有效语音内容"
ERR_EMBED_FAILED = "向量化失败"
ERR_NO_FILE = "视频原片缺失，无法转写"
ERR_ASR_TIMEOUT = "语音转写超时，请稍后重试"


def is_video_ext(ext: str | None) -> bool:
    return (ext or "").lower().lstrip(".") in VIDEO_EXTENSIONS
