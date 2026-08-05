from __future__ import annotations



from functools import lru_cache

from typing import Any

from uuid import uuid4



from qdrant_client import QdrantClient

from qdrant_client.http import models as qm



from common.config import Settings, get_settings

from common.errors import AppError, ErrorCode

from common.logging import get_logger



logger = get_logger(__name__)





class QdrantKnowledgeStore:

    def __init__(self, settings: Settings | None = None) -> None:

        self.settings = settings or get_settings()

        kwargs: dict[str, Any] = {"url": self.settings.qdrant_url}

        if self.settings.qdrant_api_key:

            kwargs["api_key"] = self.settings.qdrant_api_key

        self.client = QdrantClient(**kwargs)

        self.collection = self.settings.qdrant_collection

        try:

            self.client.get_collections()

        except Exception as exc:  # noqa: BLE001

            raise AppError(

                ErrorCode.INTERNAL,

                (

                    "cannot connect to Qdrant at "

                    f"{self.settings.qdrant_url}; start Docker service "

                    "`docker compose -f deploy/docker-compose.yml up -d qdrant` "

                    f"({exc})"

                ),

                status_code=503,

            ) from exc



    def ensure_collection(self, vector_size: int) -> None:

        existing = {c.name for c in self.client.get_collections().collections}

        if self.collection in existing:

            info = self.client.get_collection(self.collection)

            try:

                current = info.config.params.vectors.size  # type: ignore[union-attr]

            except Exception:  # noqa: BLE001

                current = vector_size

            if current != vector_size:

                raise AppError(

                    ErrorCode.INTERNAL,

                    (

                        f"qdrant collection '{self.collection}' dim={current}, "

                        f"embedding dim={vector_size}; recreate collection or align embedding"

                    ),

                    status_code=500,

                )

            return



        self.client.create_collection(

            collection_name=self.collection,

            vectors_config=qm.VectorParams(size=vector_size, distance=qm.Distance.COSINE),

        )

        logger.info("created qdrant collection=%s dim=%s", self.collection, vector_size)



    def upsert_chunks(

        self,

        *,

        public_id: str,

        kb_id: str,

        name: str,

        source: str,

        tags: list[str] | None,

        chunks: list[str],

        vectors: list[list[float]],

    ) -> list[str]:

        if len(chunks) != len(vectors):

            raise AppError(ErrorCode.INTERNAL, "chunks/vectors size mismatch", status_code=500)

        self.ensure_collection(len(vectors[0]))

        point_ids: list[str] = []

        points: list[qm.PointStruct] = []

        for idx, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):

            pid = str(uuid4())

            point_ids.append(pid)

            points.append(

                qm.PointStruct(

                    id=pid,

                    vector=vector,

                    payload={

                        "doc_id": public_id,

                        "kb_id": kb_id,

                        "name": name,

                        "source": source,

                        "chunk_index": idx,

                        "content": content,

                        "tags": tags or [],

                    },

                )

            )

        self.client.upsert(collection_name=self.collection, points=points, wait=True)

        return point_ids



    def list_chunks_by_doc_id(
        self,
        public_id: str,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """按 doc_id 拉回入库文本块（用于资料预览）。"""
        existing = {c.name for c in self.client.get_collections().collections}
        if self.collection not in existing:
            return []

        out: list[dict[str, Any]] = []
        offset = None
        while len(out) < limit:
            batch_limit = min(100, limit - len(out))
            points, offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=qm.Filter(
                    must=[
                        qm.FieldCondition(
                            key="doc_id",
                            match=qm.MatchValue(value=public_id),
                        )
                    ]
                ),
                limit=batch_limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                out.append(
                    {
                        "chunkIndex": int(payload.get("chunk_index") or 0),
                        "content": str(payload.get("content") or ""),
                    }
                )
            if offset is None or not points:
                break

        out.sort(key=lambda x: x["chunkIndex"])
        return out

    def delete_by_doc_id(self, public_id: str) -> None:

        existing = {c.name for c in self.client.get_collections().collections}

        if self.collection not in existing:

            return

        self.client.delete(

            collection_name=self.collection,

            points_selector=qm.FilterSelector(

                filter=qm.Filter(

                    must=[

                        qm.FieldCondition(

                            key="doc_id",

                            match=qm.MatchValue(value=public_id),

                        )

                    ]

                )

            ),

        )



    def delete_by_kb_id(self, kb_id: str) -> None:

        existing = {c.name for c in self.client.get_collections().collections}

        if self.collection not in existing:

            return

        self.client.delete(

            collection_name=self.collection,

            points_selector=qm.FilterSelector(

                filter=qm.Filter(

                    must=[

                        qm.FieldCondition(

                            key="kb_id",

                            match=qm.MatchValue(value=kb_id),

                        )

                    ]

                )

            ),

        )



    def search(

        self,

        *,

        vector: list[float],

        top_k: int = 5,

        min_score: float = 0.0,

        kb_id: str | None = None,

        kb_ids: list[str] | None = None,

    ) -> list[dict[str, Any]]:

        existing = {c.name for c in self.client.get_collections().collections}

        if self.collection not in existing:

            return []

        self.ensure_collection(len(vector))

        query_filter = None

        ids = [x for x in (kb_ids or []) if x]
        if not ids and kb_id:
            ids = [kb_id]

        if ids:
            if len(ids) == 1:
                match = qm.MatchValue(value=ids[0])
            else:
                match = qm.MatchAny(any=ids)
            query_filter = qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="kb_id",
                        match=match,
                    )
                ]
            )

        response = self.client.query_points(

            collection_name=self.collection,

            query=vector,

            query_filter=query_filter,

            limit=top_k,

            with_payload=True,

            score_threshold=min_score if min_score > 0 else None,

        )

        out: list[dict[str, Any]] = []

        for hit in response.points:

            payload = hit.payload or {}

            out.append(

                {

                    "content": payload.get("content", ""),

                    "score": float(hit.score or 0.0),

                    "doc_id": payload.get("doc_id"),

                    "kb_id": payload.get("kb_id"),

                    "name": payload.get("name"),

                    "chunk_index": payload.get("chunk_index"),

                    "tags": payload.get("tags") or [],

                }

            )

        return out





@lru_cache

def get_qdrant_store() -> QdrantKnowledgeStore:

    return QdrantKnowledgeStore()


