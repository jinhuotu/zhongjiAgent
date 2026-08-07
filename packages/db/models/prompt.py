from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Prompt(Base):
    """可管理的系统提示词（对话页按需选择；未选则不传 system base）。"""

    __tablename__ = "prompts"
    __table_args__ = {"comment": "系统提示词"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="提示词名称")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="提示词正文")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="创建人用户ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )
