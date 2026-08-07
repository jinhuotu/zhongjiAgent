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
    __table_args__ = {"comment": "车式窑台账"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    code: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="窑炉编码"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="窑炉名称")
    kiln_no: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="窑号")  # 如 3#
    type: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="窑型")
    workshop: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="车间")
    capacity: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="产能说明")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle", comment="状态")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
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


class KilnProcessSample(Base):
    """单窑分钟级宽表：温度区 + 压力/流量 merge 后一行。"""

    __tablename__ = "kiln_process_samples"
    __table_args__ = (
        UniqueConstraint("kiln_code", "ts", name="uq_kiln_process_samples_kiln_ts"),
        {"comment": "窑炉分钟级工况样本"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    kiln_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True, comment="窑炉编码")
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, index=True, comment="采样时间"
    )

    # 全局设定
    temp_sp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="温度设定")
    afr_sp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="空燃比设定")

    # 一～六区：温度 / 燃气流量 / 助燃风流量 / 助燃风阀位
    z1_temp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="一区温度")
    z1_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="一区燃气流量")
    z1_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="一区助燃风流量")
    z1_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True, comment="一区助燃风阀位")
    z2_temp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="二区温度")
    z2_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="二区燃气流量")
    z2_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="二区助燃风流量")
    z2_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True, comment="二区助燃风阀位")
    z3_temp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="三区温度")
    z3_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="三区燃气流量")
    z3_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="三区助燃风流量")
    z3_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True, comment="三区助燃风阀位")
    z4_temp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="四区温度")
    z4_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="四区燃气流量")
    z4_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="四区助燃风流量")
    z4_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True, comment="四区助燃风阀位")
    z5_temp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="五区温度")
    z5_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="五区燃气流量")
    z5_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="五区助燃风流量")
    z5_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True, comment="五区助燃风阀位")
    z6_temp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="六区温度")
    z6_gas_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="六区燃气流量")
    z6_air_flow: Mapped[float | None] = mapped_column(Float, nullable=True, comment="六区助燃风流量")
    z6_air_valve: Mapped[float | None] = mapped_column(Float, nullable=True, comment="六区助燃风阀位")

    # 压力 sheet
    furnace_p_sp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="炉压设定")
    furnace_p_meas: Mapped[float | None] = mapped_column(Float, nullable=True, comment="炉压实测")
    furnace_p_out: Mapped[float | None] = mapped_column(Float, nullable=True, comment="炉压输出")
    gas_p_sp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="燃气压力设定")
    gas_p_meas: Mapped[float | None] = mapped_column(Float, nullable=True, comment="燃气压力实测")
    gas_p_out: Mapped[float | None] = mapped_column(Float, nullable=True, comment="燃气压力输出")
    air_p_sp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="助燃风压力设定")
    air_p_meas: Mapped[float | None] = mapped_column(Float, nullable=True, comment="助燃风压力实测")
    air_p_out: Mapped[float | None] = mapped_column(Float, nullable=True, comment="助燃风压力输出")
    gas_flow_instant: Mapped[float | None] = mapped_column(Float, nullable=True, comment="燃气瞬时流量")
    gas_flow_total: Mapped[float | None] = mapped_column(Float, nullable=True, comment="燃气累计流量")
    air_flow_instant: Mapped[float | None] = mapped_column(Float, nullable=True, comment="助燃风瞬时流量")
    air_flow_total: Mapped[float | None] = mapped_column(Float, nullable=True, comment="助燃风累计流量")
    o2_sp: Mapped[float | None] = mapped_column(Float, nullable=True, comment="氧含量设定")
    o2_meas: Mapped[float | None] = mapped_column(Float, nullable=True, comment="氧含量实测")

    source_file: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="来源文件")
    batch_no: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="批次号")
    zone_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="温区数量")

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
