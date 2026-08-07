from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class McpServer(Base):
    """MCP Server 配置（Admin 管理；对话侧消费 enabled + tools）。"""

    __tablename__ = "mcp_servers"
    __table_args__ = {"comment": "MCP 服务配置"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="服务名称")
    # streamable_http | sse | stdio
    transport: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="传输：stdio/sse/streamable_http"
    )
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="服务URL")
    command: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="stdio命令")
    args: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="命令参数JSON")
    env: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="环境变量JSON")
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="请求头JSON")
    timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=60.0, comment="超时秒数"
    )
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最近错误")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近探测时间"
    )
    tools_cached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="工具缓存时间"
    )
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

    tools: Mapped[list[McpTool]] = relationship(
        "McpTool",
        back_populates="server",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class McpTool(Base):
    """MCP 工具缓存（由 list_tools 刷新；可按工具启停）。"""

    __tablename__ = "mcp_tools"
    __table_args__ = {"comment": "MCP 工具缓存"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    server_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属MCP服务ID",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True, comment="工具名")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="工具描述")
    input_schema: Mapped[dict | list | None] = mapped_column(
        JSON, nullable=True, comment="入参Schema JSON"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
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

    server: Mapped[McpServer] = relationship("McpServer", back_populates="tools")
