from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.ai.llm import LLMClient
from api.services.knowledge.embeddings import EmbeddingClient
from api.services.models import configs as model_configs
from common.errors import AppError, ErrorCode


async def build_llm_client(db: AsyncSession, mode: str = "fast") -> LLMClient:
    cfg = await model_configs.resolve_active_llm(db, mode)
    if cfg is None:
        other = "fast" if mode == "deep" else "deep"
        cfg = await model_configs.resolve_active_llm(db, other)
    if cfg is None:
        raise AppError(
            ErrorCode.INTERNAL,
            "LLM not configured: 请在「模型管理」中配置对话模型，并为快速/深度问答勾选用途",
            status_code=503,
        )
    return LLMClient(
        api_base=cfg.api_base,
        api_key=cfg.api_key,
        model=cfg.model_name,
        temperature=cfg.temperature,
        timeout_seconds=cfg.timeout_seconds,
        mode=mode,
    )


async def build_embedding_client(db: AsyncSession) -> EmbeddingClient:
    cfg = await model_configs.resolve_active_embedding(db)
    if cfg is None:
        raise AppError(
            ErrorCode.INTERNAL,
            "Embedding not configured: 请在「模型管理」中新增 Embedding 模型并勾选「用于知识库」",
            status_code=503,
        )
    return EmbeddingClient(
        api_base=cfg.api_base,
        api_key=cfg.api_key,
        model=cfg.model_name,
        dim=int(cfg.embedding_dim or 1536),
    )
