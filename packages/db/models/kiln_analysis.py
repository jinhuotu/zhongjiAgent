from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class KilnAnalysisRun(Base):
    """独立保存用户选型、试算参数和 AI 看板快照，不修改原始工况。"""

    __tablename__ = "kiln_analysis_runs"
    __table_args__ = (Index("ix_kiln_analysis_runs_owner_cycle", "user_id", "cycle_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cycle_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    request: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
