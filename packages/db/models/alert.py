from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class AlertRule(Base):
    """告警中心规则配置。"""

    __tablename__ = "alert_rules"
    __table_args__ = {"comment": "告警中心规则"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="规则名称")
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="工艺", comment="类型"
    )
    expression: Mapped[str] = mapped_column(String(512), nullable=False, comment="表达式")
    threshold: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="阈值")
    channels: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="通知渠道")
    devices: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="生效设备")
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用"
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
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
