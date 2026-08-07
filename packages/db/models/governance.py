"""数据治理任务：Excel 导入预览落库，供页面与对话引用。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class GovTask(Base):
    __tablename__ = "gov_tasks"
    __table_args__ = {"comment": "数据治理任务"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="任务名称")
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="任务说明")
    owner: Mapped[str] = mapped_column(String(64), nullable=False, default="管理员", comment="负责人")
    source_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="excel", comment="数据来源类型"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", comment="状态")
    # excel 预览：{ fileName, sheetName, headers, rows, rowCount, importedAt }
    excel_preview: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="Excel预览JSON")
    # 原文检索用拼接文本（表头+样例行）
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="检索拼接文本")
    file_key: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="文件存储键")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="创建人用户ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )
