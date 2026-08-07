from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from db.models.role import Role


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"comment": "系统用户"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, comment="登录用户名"
    )
    email: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, comment="邮箱"
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="密码哈希"
    )
    display_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="显示名称"
    )
    department: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="部门"
    )
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="手机号")
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用"
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否超级管理员"
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近登录时间"
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

    roles: Mapped[list[Role]] = relationship(
        "Role",
        secondary="user_roles",
        back_populates="users",
        lazy="selectin",
    )
