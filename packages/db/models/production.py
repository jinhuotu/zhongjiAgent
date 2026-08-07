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
    __table_args__ = {"comment": "生产侧系统（SCADA）"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    code: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="系统编码"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="系统名称")
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="类型：tunnel/batching/shuttle_flue"
    )  # tunnel | batching | shuttle_flue
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="说明")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="扩展元数据JSON")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )


class ProdTag(Base):
    __tablename__ = "prod_tags"
    __table_args__ = (
        UniqueConstraint("system_code", "tag_code", name="uq_prod_tags_system_tag"),
        {"comment": "生产测点定义"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    system_code: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="系统编码"
    )
    tag_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="测点编码")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="测点名称")
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="单位")
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分组")
    data_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="number", comment="数据类型"
    )
    writable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否可写")
    alarm_lo: Mapped[float | None] = mapped_column(Float, nullable=True, comment="报警下限")
    alarm_hi: Mapped[float | None] = mapped_column(Float, nullable=True, comment="报警上限")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="排序")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")


class ProdSample(Base):
    """测点时序（模拟/历史）。"""

    __tablename__ = "prod_samples"
    __table_args__ = (
        Index("ix_prod_samples_sys_tag_ts", "system_code", "tag_code", "ts"),
        UniqueConstraint("system_code", "tag_code", "ts", name="uq_prod_samples_sys_tag_ts"),
        {"comment": "生产测点时序样本"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    system_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="系统编码")
    tag_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="测点编码")
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, comment="采样时间"
    )
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True, comment="数值")
    value_text: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="文本值")


class ProdAlarm(Base):
    __tablename__ = "prod_alarms"
    __table_args__ = {"comment": "生产报警"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    system_code: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="系统编码"
    )
    tag_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="测点编码")
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="warning", comment="级别")
    title: Mapped[str] = mapped_column(String(256), nullable=False, comment="标题")
    message: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="详情")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", comment="状态：active/acked/closed"
    )  # active|acked|closed
    raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="产生时间"
    )
    acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认时间"
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="关闭时间"
    )
    acked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="确认人用户ID")
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="扩展JSON")


class ProdCommand(Base):
    """控制下发：一期模拟执行，执行器接口预留。"""

    __tablename__ = "prod_commands"
    __table_args__ = {"comment": "生产控制指令"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="对外ID"
    )
    system_code: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, comment="系统编码"
    )
    tag_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="测点编码")
    action: Mapped[str] = mapped_column(String(32), nullable=False, default="write", comment="动作")
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True, comment="目标数值")
    target_text: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="目标文本")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", comment="状态"
    )  # pending|simulated|sent|success|failed
    executor: Mapped[str] = mapped_column(
        String(32), nullable=False, default="simulate", comment="执行器"
    )
    result_msg: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="执行结果")
    requested_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="请求人用户ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
