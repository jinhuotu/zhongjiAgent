"""操作日志与登录日志（滚动保留，默认 7 天）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="创建时间",
    )
