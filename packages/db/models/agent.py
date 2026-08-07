from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ScenarioAgent(Base):
    """场景智能体：提示词 + 知识库 + 模式 + MCP 工具白名单的配置包。"""

    __tablename__ = "scenario_agents"
    __table_args__ = {"comment": "场景智能体配置"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="智能体名称")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    # prompts.public_id；空 = 对话不注入管理提示词
    prompt_public_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="绑定提示词ID"
    )
    knowledge_base_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="知识库ID列表"
    )
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fast", comment="模式：fast/deep"
    )
    # mcp_tools.public_id 列表；空/null = 不额外限制（使用全部已启用工具）
    mcp_tool_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="MCP工具白名单")
    tools_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用工具调用"
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
