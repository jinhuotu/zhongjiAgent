"""操作日志、登录日志，以及知识库变更 / 数据查询审计（后两者不按 7 天滚动删除）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class LoginLog(Base):
    __tablename__ = "login_logs"
    __table_args__ = {"comment": "登录日志"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="尝试登录的用户名"
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="对应用户ID（未知则为空）"
    )
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True, comment="是否成功"
    )
    reason: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="结果原因：ok/invalid_credentials/inactive"
    )
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="客户端IP")
    user_agent: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="User-Agent"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="创建时间",
    )


class OperationLog(Base):
    __tablename__ = "operation_logs"
    __table_args__ = {"comment": "操作日志"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    module: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="模块：users/roles/hot_configs/mcp/models"
    )
    action: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", comment="动作：create/update/delete/..."
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False, comment="HTTP方法")
    path: Mapped[str] = mapped_column(String(512), nullable=False, comment="请求路径")
    resource_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="资源ID"
    )
    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="操作人用户ID"
    )
    operator_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="操作人用户名"
    )
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True, comment="是否成功"
    )
    status_code: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="HTTP状态码"
    )
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="客户端IP")
    user_agent: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="User-Agent"
    )
    detail: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="摘要（已脱敏）"
    )
    error_msg: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="失败原因"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="耗时毫秒"
    )
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", index=True, comment="请求ID（关联追溯）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="创建时间",
    )


class KnowledgeAudit(Base):
    """知识库结构变更（建库/改库/删库/文档/ACL），供审计追溯，不随操作日志 7 天清理。"""

    __tablename__ = "knowledge_audits"
    __table_args__ = {"comment": "知识库变更审计"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    action: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="动作：create_base/update_base/delete_base/acl_update/upload_doc/delete_doc",
    )
    base_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="知识库对外ID"
    )
    doc_id: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="文档对外ID")
    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="操作人用户ID"
    )
    operator_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="操作人用户名"
    )
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更前摘要JSON")
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更后摘要JSON")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="客户端IP")
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="请求ID"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="创建时间",
    )


class DataQueryLog(Base):
    """数据查询留痕（知识库检索、MCP 工具调用）。不随操作日志 7 天清理。"""

    __tablename__ = "data_query_logs"
    __table_args__ = {"comment": "数据查询日志"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="来源：knowledge/mcp"
    )
    query_text: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", comment="查询摘要"
    )
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="工具名")
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="参数摘要JSON")
    result_summary: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="结果摘要（截断）"
    )
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True, comment="是否成功"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="耗时毫秒"
    )
    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="查询人用户ID"
    )
    operator_username: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="查询人用户名"
    )
    session_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="关联问答会话对外ID"
    )
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="客户端IP")
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", comment="请求ID"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="创建时间",
    )
