"""清空知识库文档与向量（保留 knowledge_bases 卡片）。

用法:
  poetry run python scripts/clear_knowledge_docs.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps"))

from sqlalchemy import delete, select

from common.config import get_settings
from db.models.knowledge import KnowledgeDocument
from db.session import AsyncSessionLocal


async def main() -> None:
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(KnowledgeDocument.public_id))
        doc_ids = [r[0] for r in result.all()]
        await db.execute(delete(KnowledgeDocument))
        await db.commit()
        print(f"deleted mysql documents: {len(doc_ids)}")

    try:
        from qdrant_client import QdrantClient

        kwargs = {"url": settings.qdrant_url}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        client = QdrantClient(**kwargs)
        cols = {c.name for c in client.get_collections().collections}
        if settings.qdrant_collection in cols:
            client.delete_collection(settings.qdrant_collection)
            print(f"deleted qdrant collection: {settings.qdrant_collection}")
        else:
            print("qdrant collection not found, skip")
    except Exception as exc:  # noqa: BLE001
        print(f"qdrant clear skipped: {exc}")

    for p in (
        Path("/tmp/lujing-kb/index.json"),
        Path("C:/tmp/lujing-kb/index.json"),
    ):
        if p.exists():
            p.write_text("[]", encoding="utf-8")
            print(f"cleared local index: {p}")


if __name__ == "__main__":
    asyncio.run(main())
