from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from db.models.user import User


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = {"comment": "角色"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="角色编码")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="角色名称")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="角色说明")
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

    users: Mapped[list[User]] = relationship(
        "User",
        secondary="user_roles",
        back_populates="roles",
        lazy="selectin",
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
        {"comment": "用户-角色关联"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="用户ID",
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        comment="角色ID",
    )
