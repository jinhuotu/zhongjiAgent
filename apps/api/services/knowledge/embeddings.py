from __future__ import annotations

import hashlib
import math
import re

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import get_settings
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


def _format_http_error(resp: httpx.Response) -> str:
    """尽量取出网关返回的可读错误，避免只剩 Client error '400'。"""
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("msg") or err)
            else:
                detail = str(
                    data.get("message") or data.get("msg") or data.get("detail") or ""
                )
    except Exception:  # noqa: BLE001
        detail = (resp.text or "")[:400]
    detail = detail.strip()
    if detail:
        return f"{resp.status_code} {resp.reason_phrase}: {detail}"
    return f"{resp.status_code} {resp.reason_phrase}"


class EmbeddingClient:
    """Vector model client driven by 模型管理 DB config (no .env fallback)."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        dim: int = 1536,
        batch_size: int | None = None,
        max_chars: int | None = None,
    ) -> None:
        settings = get_settings()
        self.dim = int(dim or 1536)
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._model = model
        self._batch_size = max(1, int(batch_size or settings.embedding_batch_size or 8))
        self._max_chars = max(256, int(max_chars or settings.embedding_max_chars or 6000))
        # 部分网关不接受 dimensions；首次 400 后对该 client 永久跳过
        self._send_dimensions = self.dim > 0
        if not self._api_key or not self._api_base:
            raise AppError(
                ErrorCode.INTERNAL,
                "Embedding not configured: 请在「模型管理」中配置并启用 Embedding 模型",
                status_code=503,
            )
        logger.info(
            "embedding provider=remote model=%s base=%s dim=%s batch=%s",
            self._model,
            self._api_base,
            self.dim,
            self._batch_size,
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
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed cache bypass: %s", exc)
            return await self._remote_embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed_documents([text])
        return vectors[0]

    def _prepare_texts(self, texts: list[str]) -> list[str]:
        out: list[str] = []
        for t in texts:
            s = (t or "").strip() or " "
            if len(s) > self._max_chars:
                logger.warning(
                    "embedding truncate chars=%s -> %s",
                    len(s),
                    self._max_chars,
                )
                s = s[: self._max_chars]
            out.append(s)
        return out

    async def _remote_embed(self, texts: list[str]) -> list[list[float]]:
        prepared = self._prepare_texts(texts)
        if len(prepared) <= self._batch_size:
            return await self._remote_embed_batch(prepared)

        vectors: list[list[float]] = []
        total = len(prepared)
        for start in range(0, total, self._batch_size):
            batch = prepared[start : start + self._batch_size]
            logger.info(
                "embedding batch %s-%s / %s",
                start + 1,
                start + len(batch),
                total,
            )
            vectors.extend(await self._remote_embed_batch(batch))
        return vectors

    async def _remote_embed_batch(self, texts: list[str]) -> list[list[float]]:
        """单批调用；批量 400 时降级为逐条，兼容严格网关。"""
        try:
            return await self._post_embeddings(texts)
        except AppError as exc:
            if len(texts) <= 1 or "400" not in str(exc.msg):
                raise
            logger.warning(
                "embedding batch size=%s got 400; fallback to single input",
                len(texts),
            )
            vectors: list[list[float]] = []
            for one in texts:
                vectors.extend(await self._post_embeddings([one]))
            return vectors

    async def _post_embeddings(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._api_base}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self._model,
            "input": texts,
        }
        if self._send_dimensions:
            payload["dimensions"] = self.dim

        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 400 and "dimensions" in payload:
                    # 网关不支持 dimensions：去掉后重试，并记住不再发送
                    payload.pop("dimensions", None)
                    self._send_dimensions = False
                    logger.info("embedding retry without dimensions")
                    resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 400:
                    raise AppError(
                        ErrorCode.INTERNAL,
                        f"embedding request failed: {_format_http_error(resp)}",
                        status_code=502,
                    )
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
        if vectors:
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
