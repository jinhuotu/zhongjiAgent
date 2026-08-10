from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class BizReport(Base):
    """业务统计报表（非 AI 叙事报告）。"""

    __tablename__ = "biz_reports"
    __table_args__ = {"comment": "业务统计报表"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False, comment="报表名称")
    report_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="类型：日报/周报/月报等"
    )
    period: Mapped[str] = mapped_column(String(64), nullable=False, comment="统计周期")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="generating",
        comment="状态：generating/ready/pending_review",
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="报表正文")
    char_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="字符数"
    )
    size_label: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="展示用大小"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="创建人用户ID"
    )
    created_by_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建人显示名"
    )
    template_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="模板键"
    )
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="失败原因")
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
