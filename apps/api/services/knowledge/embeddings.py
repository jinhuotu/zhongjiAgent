from __future__ import annotations

import hashlib
import math
import re

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from common.errors import AppError, ErrorCode
from common.logging import get_logger

logger = get_logger(__name__)

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text) if t.strip()]


def local_hash_embed(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    tokens = _tokenize(text) or ["empty"]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for i in range(0, min(len(digest), 32), 4):
            idx = int.from_bytes(digest[i : i + 4], "little") % dim
            sign = 1.0 if digest[i] % 2 == 0 else -1.0
            vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class EmbeddingClient:
    """Vector model client driven by 模型管理 DB config (no .env fallback)."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        dim: int = 1536,
    ) -> None:
        self.dim = int(dim or 1536)
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._model = model
        if not self._api_key or not self._api_base:
            raise AppError(
                ErrorCode.INTERNAL,
                "Embedding not configured: 请在「模型管理」中配置并启用 Embedding 模型",
                status_code=503,
            )
        logger.info(
            "embedding provider=remote model=%s base=%s dim=%s",
            self._model,
            self._api_base,
            self.dim,
        )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Redis String 缓存：命中则跳过远端调用
        try:
            from common.redis_tools import (
                get_cached_embeddings_batch,
                set_cached_embeddings_batch,
            )

            cached, missing_idx = await get_cached_embeddings_batch(self._model, texts)
            if not missing_idx:
                return [v for v in cached if v is not None]
            to_embed = [texts[i] for i in missing_idx]
            fresh = await self._remote_embed(to_embed)
            await set_cached_embeddings_batch(self._model, to_embed, fresh)
            for i, vec in zip(missing_idx, fresh, strict=True):
                cached[i] = vec
            return [v if v is not None else [] for v in cached]
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed cache bypass: %s", exc)
            return await self._remote_embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed_documents([text])
        return vectors[0]

    async def _remote_embed(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._api_base}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self._model,
            "input": texts,
        }
        if self.dim > 0:
            payload["dimensions"] = self.dim

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 400 and "dimensions" in payload:
                    payload.pop("dimensions", None)
                    resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.INTERNAL,
                f"embedding request failed: {exc}",
                status_code=502,
            ) from exc

        items = data.get("data") or []
        items = sorted(items, key=lambda x: x.get("index", 0))
        vectors = [list(map(float, it["embedding"])) for it in items]
        if len(vectors) != len(texts):
            raise AppError(ErrorCode.INTERNAL, "embedding response size mismatch", status_code=502)
        actual_dim = len(vectors[0])
        if actual_dim != self.dim:
            logger.warning(
                "embedding dim mismatch config=%s actual=%s; using actual",
                self.dim,
                actual_dim,
            )
            self.dim = actual_dim
        return vectors


async def get_embedding_client(db: AsyncSession) -> EmbeddingClient:
    from api.services.models.runtime import build_embedding_client

    return await build_embedding_client(db)
