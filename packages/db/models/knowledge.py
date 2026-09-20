from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class KnowledgeBase(Base):
    """用户自定义知识库（卡片列表实体）。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = {"comment": "知识库"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="知识库名称")
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="描述")
    # rag=AI 知识库；历史 asset 行仅过滤不展示
    purpose: Mapped[str] = mapped_column(
        String(16), nullable=False, default="rag", index=True, comment="用途：rag=AI知识库"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", comment="状态")
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

    documents: Mapped[list[KnowledgeDocument]] = relationship(
        "KnowledgeDocument",
        back_populates="base",
        lazy="selectin",
    )
    acl_entries: Mapped[list["KnowledgeBaseAcl"]] = relationship(
        "KnowledgeBaseAcl",
        back_populates="base",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class KnowledgeBaseAcl(Base):
    """知识库访问授权（查看 / 使用 / 维护）。"""

    __tablename__ = "knowledge_base_acl"
    __table_args__ = (
        UniqueConstraint("base_id", "subject_type", "subject_id", name="uq_kb_acl_subject"),
        {"comment": "知识库访问授权"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    base_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="知识库ID",
    )
    subject_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="主体：user / role"
    )
    subject_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="users.id 或 roles.id"
    )
    can_view: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="列表与详情可见"
    )
    can_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="对话检索 / 语义检索"
    )
    can_manage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="改库、导入删除资料、分配权限"
    )

    base: Mapped[KnowledgeBase] = relationship("KnowledgeBase", back_populates="acl_entries")


class KnowledgeDocument(Base):
    """知识库文档元数据（向量存 Qdrant）。"""

    __tablename__ = "knowledge_documents"
    __table_args__ = {"comment": "知识库文档元数据"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    base_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属知识库ID",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="资料名称")
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="text", comment="来源：file/text/url"
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="doc", comment="类型：doc/3d/video/image"
    )
    file_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="文件扩展名")
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="文件大小(字节)")
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="来源URL")
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="存储相对路径")
    # 对齐前端 KbItem.fileKey / previewUrl（三维预览等）
    file_key: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="文件键")
    preview_url: Mapped[str | None] = mapped_column(String(2048), nullable=True, comment="预览地址")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="摘要")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="字符数")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="向量切块数")
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="标签JSON")
    uploader: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="上传人")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="parsing", comment="状态：parsing/ready/failed"
    )
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
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

    base: Mapped[KnowledgeBase] = relationship("KnowledgeBase", back_populates="documents")
