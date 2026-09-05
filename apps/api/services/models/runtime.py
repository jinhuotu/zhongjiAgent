from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.ai.llm import LLMClient
from api.services.knowledge.embeddings import EmbeddingClient
from api.services.models import configs as model_configs
from common.errors import AppError, ErrorCode


async def build_llm_client(
    db: AsyncSession,
    mode: str = "fast",
    *,
    model_id: str | None = None,
) -> LLMClient:
    if model_id and str(model_id).strip():
        return await build_llm_client_by_id(db, str(model_id).strip(), mode=mode)

    cfg = await model_configs.resolve_active_llm(db, mode)
    if cfg is None:
        other = "fast" if mode == "deep" else "deep"
        cfg = await model_configs.resolve_active_llm(db, other)
    if cfg is None:
        cfg = await model_configs.resolve_first_enabled_llm(db)
    if cfg is None:
        raise AppError(
            ErrorCode.INTERNAL,
            "LLM not configured: 请在「模型管理」中新增并启用对话模型",
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


async def build_llm_client_by_id(
    db: AsyncSession,
    public_id: str,
    *,
    prefer_model_type: str | None = None,
    mode: str = "fast",
) -> LLMClient:
    """按模型配置 public_id 构建客户端（用于图纸识参指定多模态视觉等）。"""
    cfg = await model_configs.get_by_public_id(db, public_id)
    if not cfg.enabled:
        raise AppError(ErrorCode.VALIDATION, "所选模型未启用", status_code=422)
    if cfg.kind != "llm":
        raise AppError(ErrorCode.VALIDATION, "所选配置不是对话/多模态模型", status_code=422)
    model_type = model_configs.normalize_model_type(
        cfg.kind, getattr(cfg, "model_type", None)
    )
    if prefer_model_type and model_type != prefer_model_type:
        raise AppError(
            ErrorCode.VALIDATION,
            f"请选择类型为「{prefer_model_type}」的模型（当前为 {model_type}）",
            status_code=422,
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
