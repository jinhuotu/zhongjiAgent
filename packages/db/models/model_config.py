from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ModelConfig(Base):
    """OpenAI 兼容模型配置（LLM / Embedding）。"""

    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # llm | embedding
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    api_base: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(512), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=120.0)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 业务绑定：同一用途建议仅一条为 True
    scope_fast: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scope_deep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scope_embedding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
