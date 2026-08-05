"""生产侧 SCADA：隧道窑 / 配料楼 / 梭式窑烟气（统一测点 + 报警 + 控制指令）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ProdSystem(Base):
    __tablename__ = "prod_systems"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # tunnel | batching | shuttle_flue
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProdTag(Base):
    __tablename__ = "prod_tags"
    __table_args__ = (
        UniqueConstraint("system_code", "tag_code", name="uq_prod_tags_system_tag"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    system_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tag_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_type: Mapped[str] = mapped_column(String(16), nullable=False, default="number")
    writable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    alarm_lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    alarm_hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProdSample(Base):
    """测点时序（模拟/历史）。"""

    __tablename__ = "prod_samples"
    __table_args__ = (
        Index("ix_prod_samples_sys_tag_ts", "system_code", "tag_code", "ts"),
        UniqueConstraint("system_code", "tag_code", "ts", name="uq_prod_samples_sys_tag_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    system_code: Mapped[str] = mapped_column(String(32), nullable=False)
    tag_code: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(256), nullable=True)


class ProdAlarm(Base):
    __tablename__ = "prod_alarms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    system_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tag_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active|acked|closed
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ProdCommand(Base):
    """控制下发：一期模拟执行，执行器接口预留。"""

    __tablename__ = "prod_commands"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    system_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tag_code: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="write")
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_text: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending|simulated|sent|success|failed
    executor: Mapped[str] = mapped_column(String(32), nullable=False, default="simulate")
    result_msg: Mapped[str | None] = mapped_column(String(512), nullable=True)
    requested_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
