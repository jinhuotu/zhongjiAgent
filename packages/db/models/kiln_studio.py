"""Versioned studio manifests; large immutable data/artifacts live in storage."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class KilnStudioAsset(Base):
    __tablename__ = "kiln_studio_assets"
    __table_args__ = (Index("ix_kiln_studio_owner_project", "user_id", "project_id", "kind"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
