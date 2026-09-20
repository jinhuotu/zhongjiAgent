"""从视频抽出音轨，方便送进 Whisper（体积小、兼容性好）。依赖本机 ffmpeg。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from common.errors import AppError, ErrorCode

logger = logging.getLogger("api.kb.asr.audio")

# OpenAI / 多数兼容服务单文件上限约 25MB
_ASR_UPLOAD_MAX = 24 * 1024 * 1024


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def prepare_asr_media(video_path: Path) -> tuple[Path, bool]:
    """返回 (送 ASR 的文件路径, 是否临时文件需删除)。优先抽 mp3。"""
    if not video_path.is_file():
        raise AppError(ErrorCode.NOT_FOUND, "视频原片不存在", status_code=404)

    ff = ffmpeg_bin()
    if ff:
        tmp = tempfile.NamedTemporaryFile(prefix="kb-asr-", suffix=".mp3", delete=False)
        tmp.close()
        out = Path(tmp.name)
        try:
            cmd = [
                ff,
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(out),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )
            if proc.returncode != 0 or not out.is_file() or out.stat().st_size <= 0:
                out.unlink(missing_ok=True)
                err = (proc.stderr or proc.stdout or "").strip()[-400:]
                raise AppError(
                    ErrorCode.VALIDATION,
                    f"抽取音轨失败{('：' + err) if err else ''}",
                    status_code=422,
                )
            if out.stat().st_size > _ASR_UPLOAD_MAX:
                out.unlink(missing_ok=True)
                raise AppError(
                    ErrorCode.VALIDATION,
                    "音轨过大，无法送转写服务（请缩短视频或提高压缩）",
                    status_code=422,
                )
            return out, True
        except AppError:
            raise
        except subprocess.TimeoutExpired as exc:
            out.unlink(missing_ok=True)
            raise AppError(ErrorCode.VALIDATION, "抽取音轨超时", status_code=422) from exc
        except Exception as exc:  # noqa: BLE001
            out.unlink(missing_ok=True)
            logger.exception("ffmpeg extract failed")
            raise AppError(
                ErrorCode.VALIDATION,
                f"抽取音轨失败：{exc}",
                status_code=422,
            ) from exc

    size = video_path.stat().st_size
    if size > _ASR_UPLOAD_MAX:
        raise AppError(
            ErrorCode.VALIDATION,
            "未安装 ffmpeg，且视频超过转写上传上限；请安装 ffmpeg 后重试",
            status_code=422,
        )
    return video_path, False
