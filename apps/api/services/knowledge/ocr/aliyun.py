"""阿里云通用文字识别（ocr-api），RecognizeAllText / Type=Advanced。"""

from __future__ import annotations

import io
import logging

from common.errors import AppError, ErrorCode

from api.services.knowledge.ocr.image_prep import to_ocr_jpeg

logger = logging.getLogger("api.kb.ocr")


class AliyunOcrProvider:
    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        endpoint: str,
        ocr_type: str = "Advanced",
        timeout_seconds: int = 20,
    ) -> None:
        if not access_key_id.strip() or not access_key_secret.strip():
            raise AppError(
                ErrorCode.VALIDATION,
                "未配置 OCR_ACCESS_KEY_ID / OCR_ACCESS_KEY_SECRET",
                status_code=422,
            )
        self._ak = access_key_id.strip()
        self._sk = access_key_secret.strip()
        self._endpoint = (endpoint or "ocr-api.cn-hangzhou.aliyuncs.com").strip()
        self._type = (ocr_type or "Advanced").strip() or "Advanced"
        self._timeout_ms = max(5, int(timeout_seconds)) * 1000
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from alibabacloud_ocr_api20210707.client import Client as OcrClient
                from alibabacloud_tea_openapi import models as open_api_models
            except ImportError as exc:
                raise AppError(
                    ErrorCode.VALIDATION,
                    "未安装阿里云 OCR SDK。请 poetry add alibabacloud-ocr-api20210707 后重试",
                    status_code=422,
                ) from exc
            config = open_api_models.Config(
                access_key_id=self._ak,
                access_key_secret=self._sk,
                endpoint=self._endpoint,
            )
            config.connect_timeout = self._timeout_ms
            config.read_timeout = self._timeout_ms
            self._client = OcrClient(config)
        return self._client

    def recognize(self, image: bytes) -> str:
        from alibabacloud_ocr_api20210707 import models as ocr_models
        from alibabacloud_tea_util import models as util_models

        jpeg = to_ocr_jpeg(image)
        client = self._get_client()
        request = ocr_models.RecognizeAllTextRequest(
            body=io.BytesIO(jpeg),
            type=self._type,
        )
        runtime = util_models.RuntimeOptions()
        try:
            resp = client.recognize_all_text_with_options(request, runtime)
        except Exception as exc:  # noqa: BLE001
            msg = getattr(exc, "message", None) or str(exc)
            logger.warning("aliyun ocr failed: %s", msg)
            raise AppError(ErrorCode.INTERNAL, f"阿里云 OCR 失败：{msg}", status_code=502) from exc

        data = getattr(getattr(resp, "body", None), "data", None)
        content = (getattr(data, "content", None) or "") if data is not None else ""
        text = str(content).strip()
        logger.info("aliyun ocr ok chars=%s type=%s", len(text), self._type)
        return text
