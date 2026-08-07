from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class HotConfig(Base):
    """全局热配置（DB 权威源；Redis Hash 为只读热缓存）。"""

    __tablename__ = "hot_configs"
    __table_args__ = {"comment": "全局热配置"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    config_key: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True, comment="配置键"
    )
    value: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="配置值")
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="说明")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="版本号")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="更新人用户ID")
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


class HotConfigAudit(Base):
    """热配置变更审计（等保 / 信创留痕）。"""

    __tablename__ = "hot_config_audits"
    __table_args__ = {"comment": "热配置变更审计"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    config_key: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True, comment="配置键"
    )
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更前值")
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更后值")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="版本号")
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="update", comment="动作")
    operator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="操作人用户ID")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
