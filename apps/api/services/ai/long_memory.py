"""会话长期记忆：摘要向量写入独立 Qdrant collection。

Collection：Settings.chat_memory_collection（默认 lujing_chat_memory）
Payload：session_id / user_id / summary / created_at
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.knowledge.embeddings import get_embedding_client
from common.config import get_settings
from common.logging import get_logger

logger = get_logger(__name__)


class ChatLongMemoryStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        kwargs: dict[str, Any] = {"url": self.settings.qdrant_url}
        if self.settings.qdrant_api_key:
            kwargs["api_key"] = self.settings.qdrant_api_key
        self.client = QdrantClient(**kwargs)
        self.collection = self.settings.chat_memory_collection

    def ensure_collection(self, vector_size: int) -> None:
        cols = {c.name for c in self.client.get_collections().collections}
        if self.collection in cols:
            return
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=qm.VectorParams(size=vector_size, distance=qm.Distance.COSINE),
        )
        logger.info("created chat memory collection=%s dim=%s", self.collection, vector_size)

    def upsert_summary(
        self,
        *,
        session_id: str,
        user_id: int,
        summary: str,
        vector: list[float],
    ) -> str:
        self.ensure_collection(len(vector))
        point_id = str(uuid4())
        self.client.upsert(
            collection_name=self.collection,
            points=[
                qm.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "session_id": session_id,
                        "user_id": user_id,
                        "summary": summary,
                        "content": summary,
                    },
                )
            ],
            wait=True,
        )
        return point_id

    def search(
        self,
        *,
        user_id: int,
        vector: list[float],
        session_id: str | None = None,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        cols = {c.name for c in self.client.get_collections().collections}
        if self.collection not in cols:
            return []
        must = [
            qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
        ]
        if session_id:
            must.append(
                qm.FieldCondition(key="session_id", match=qm.MatchValue(value=session_id))
            )
        hits = self.client.search(
            collection_name=self.collection,
            query_vector=vector,
            query_filter=qm.Filter(must=must),
            limit=top_k,
            with_payload=True,
        )
        out: list[dict[str, Any]] = []
        for h in hits:
            payload = h.payload or {}
            out.append(
                {
                    "score": float(h.score or 0),
                    "summary": str(payload.get("summary") or payload.get("content") or ""),
                    "session_id": payload.get("session_id"),
                }
            )
        return out


_store: ChatLongMemoryStore | None = None


def get_long_memory_store() -> ChatLongMemoryStore:
    global _store
    if _store is None:
        _store = ChatLongMemoryStore()
    return _store


async def store_trim_summary(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: int,
    summary: str,
) -> None:
    if not summary.strip():
        return
    client = await get_embedding_client(db)
    vector = await client.embed_query(summary.strip())
    get_long_memory_store().upsert_summary(
        session_id=session_id,
        user_id=user_id,
        summary=summary.strip(),
        vector=vector,
    )


async def recall_long_memory(
    db: AsyncSession,
    *,
    user_id: int,
    query: str,
    session_id: str | None = None,
    top_k: int = 3,
) -> list[str]:
    client = await get_embedding_client(db)
    vector = await client.embed_query(query)
    hits = get_long_memory_store().search(
        user_id=user_id,
        vector=vector,
        session_id=session_id,
        top_k=top_k,
    )
    return [h["summary"] for h in hits if h.get("summary")]
