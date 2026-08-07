from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class ChatSession(Base):
    """AI 智能问答会话。"""

    __tablename__ = "chat_sessions"
    __table_args__ = {"comment": "AI 问答会话"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="用户ID")
    title: Mapped[str] = mapped_column(
        String(128), nullable=False, default="新对话", comment="会话标题"
    )
    title_auto: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否自动生成标题"
    )
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fast", comment="模式：fast/deep"
    )
    summary: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="会话摘要")
    # 最近一次对话勾选的知识库 public_id 列表（打开历史会话时恢复勾选）
    knowledge_base_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="关联知识库ID列表"
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="消息条数"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近消息时间"
    )
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

    messages: Mapped[list[ChatMessage]] = relationship(
        "ChatMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ChatMessage.id",
    )


class ChatMessage(Base):
    """会话消息（含可选 RAG 引用 / Stream 幂等 / tool 预留字段）。"""

    __tablename__ = "chat_messages"
    __table_args__ = {"comment": "AI 问答消息"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="会话ID",
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="角色：user/assistant/tool等"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="消息正文")
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="生成模式")
    refs: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="引用片段JSON")
    # Redis Stream 消息 ID，归档幂等键（如 "1712345678901-0"）
    stream_msg_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, comment="Redis Stream消息ID"
    )
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="模型名")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="提示词token数")
    completion_tokens: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="补全token数"
    )
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="总token数")
    # MCP tool 预留（M1 仅结构，不接入真实插件）
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="工具名")
    tool_input: Mapped[dict | list | None] = mapped_column(JSON, nullable=True, comment="工具入参JSON")
    tool_output: Mapped[dict | list | None] = mapped_column(JSON, nullable=True, comment="工具出参JSON")
    tool_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工具错误信息")
    tool_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="工具耗时毫秒")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )

    session: Mapped[ChatSession] = relationship("ChatSession", back_populates="messages")
