"""车式窑台账 + 分钟级历史工况（由 Excel 导入）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Furnace(Base):
    __tablename__ = "furnaces"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kiln_no: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 如 3#
    type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workshop: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capacity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class KilnProcessSample(Base):
    """单窑分钟级宽表：温度区 + 压力/流量 merge 后一行。"""

    __tablename__ = "kiln_process_samples"
    __table_args__ = (
        UniqueConstraint("kiln_code", "ts", name="uq_kiln_process_samples_kiln_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kiln_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, index=True)

    # 全局设定
    temp_sp: Mapped[float | None] = mapped_column(Float, nullable=True)
    afr_sp: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 一～六区：温度 / 燃气流量 / 助燃风流量 / 助燃风阀位
    z1_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    z1_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z1_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z1_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True)
    z2_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    z2_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z2_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z2_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True)
    z3_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    z3_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z3_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z3_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True)
    z4_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    z4_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z4_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z4_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True)
    z5_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    z5_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z5_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z5_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True)
    z6_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    z6_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z6_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    z6_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 压力 sheet
    furnace_p_sp: Mapped[float | None] = mapped_column(Float, nullable=True)
    furnace_p_meas: Mapped[float | None] = mapped_column(Float, nullable=True)
    furnace_p_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    gas_p_sp: Mapped[float | None] = mapped_column(Float, nullable=True)
    gas_p_meas: Mapped[float | None] = mapped_column(Float, nullable=True)
    gas_p_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_p_sp: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_p_meas: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_p_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    gas_flow_instant: Mapped[float | None] = mapped_column(Float, nullable=True)
    gas_flow_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_flow_instant: Mapped[float | None] = mapped_column(Float, nullable=True)
    air_flow_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    o2_sp: Mapped[float | None] = mapped_column(Float, nullable=True)
    o2_meas: Mapped[float | None] = mapped_column(Float, nullable=True)

    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True)
    batch_no: Mapped[str | None] = mapped_column(String(16), nullable=True)
    zone_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
