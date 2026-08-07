from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class AiReport(Base):
    """AI 智能报告（生成历史 + 正文）。"""

    __tablename__ = "ai_reports"
    __table_args__ = {"comment": "AI 智能报告"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="用户ID")
    report_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="报告类型"
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="", comment="标题")
    kiln_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True, comment="窑炉编码")
    kiln_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="窑炉名称")
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="deep", comment="生成模式"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="generating", comment="状态")
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="报告正文")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="字符数")
    refs: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="引用JSON")
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="上下文摘要")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="错误信息")
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
