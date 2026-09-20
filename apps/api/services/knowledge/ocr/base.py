"""OCR 提供者：输入图片 bytes，输出纯文本。"""

from __future__ import annotations

from typing import Protocol


class OcrProvider(Protocol):
    def recognize(self, image: bytes) -> str:
        """同步识别。调用方用 asyncio.to_thread 包一层。"""
        ...
