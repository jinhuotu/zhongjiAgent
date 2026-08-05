from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from common.errors import AppError, ErrorCode
from common.logging import get_logger

logger = get_logger(__name__)


class LLMClient:
    """OpenAI-compatible Chat Completions client (DB 模型管理 only; no .env fallback)."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        temperature: float | None = None,
        timeout_seconds: float = 120.0,
        mode: str = "fast",
    ) -> None:
        self.api_base = (api_base or "").rstrip("/")
        self.api_key = api_key or ""
        self.fixed_model = model
        self.fixed_temperature = temperature
        self.timeout_seconds = float(timeout_seconds or 120.0)
        self.default_mode = mode
        if not self.api_base or not self.api_key or not self.fixed_model:
            raise AppError(
                ErrorCode.INTERNAL,
                "LLM not configured: 请在「模型管理」中配置并启用对话模型（快速/深度）",
                status_code=503,
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _model(self, mode: str) -> str:
        _ = mode
        return self.fixed_model

    def _temperature(self, mode: str) -> float:
        _ = mode
        if self.fixed_temperature is not None:
            return float(self.fixed_temperature)
        return 0.6 if self.default_mode != "deep" else 0.4

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        mode: str = "fast",
    ) -> AsyncIterator[str]:
        model = self._model(mode)
        temperature = self._temperature(mode)
        url = f"{self.api_base}/chat/completions"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        logger.info("llm stream model=%s mode=%s", model, mode)

        timeout = httpx.Timeout(self.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="ignore")
                    raise AppError(
                        ErrorCode.INTERNAL,
                        f"LLM request failed ({resp.status_code}): {body[:300]}",
                        status_code=502,
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield str(text)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        mode: str = "fast",
    ) -> str:
        model = self._model(mode)
        temperature = self._temperature(mode)
        url = f"{self.api_base}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        timeout = httpx.Timeout(self.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            if resp.status_code >= 400:
                raise AppError(
                    ErrorCode.INTERNAL,
                    f"LLM request failed ({resp.status_code}): {resp.text[:300]}",
                    status_code=502,
                )
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])
