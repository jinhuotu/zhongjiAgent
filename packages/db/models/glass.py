"""玻璃瑕疵检测：检测任务 + 检测框（演示版）。

一次上传 = 一条 glass_inspection_tasks，一个检测框 = 一条 glass_defect_boxes。
复核字段（is_false_positive / is_missed / review_status）是后续"自我迭代闭环"
采集错例的唯一入口，务必保留。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class GlassInspectionTask(Base):
    """玻璃瑕疵检测任务（一张图一条）。"""

    __tablename__ = "glass_inspection_tasks"
    __table_args__ = {"comment": "玻璃瑕疵检测任务"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    task_no: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="业务编号"
    )
    image_path: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="图片相对storage_root的路径"
    )
    original_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="原始文件名"
    )
    image_width: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="原图宽")
    image_height: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="原图高")
    model_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="推理使用的模型版本"
    )
    model_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="关联模型配置public_id"
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="upload", comment="来源：upload/batch"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", comment="状态：pending/running/success/failed"
    )
    defect_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="瑕疵框数量"
    )
    max_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="最高置信度"
    )
    conclusion: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="结论（演示版）"
    )
    grade: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="等级（演示版恒为空）"
    )
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="推理耗时毫秒")
    error_message: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="失败原因"
    )
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unreviewed", comment="复核状态：unreviewed/corrected/rejected"
    )
    reviewed_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="复核人显示名"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="复核时间"
    )
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")
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


class GlassDefectBox(Base):
    """检测框：一个瑕疵一条，含人工复核标记。"""

    __tablename__ = "glass_defect_boxes"
    __table_args__ = {"comment": "玻璃瑕疵检测框"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("glass_inspection_tasks.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="所属任务ID",
    )
    cls_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="模型原始类别ID")
    cls_name_raw: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="模型原始类别名"
    )
    defect_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="映射后的标准缺陷类型"
    )
    mapped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否映射到标准缺陷类型"
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, comment="置信度")
    x1: Mapped[float] = mapped_column(Float, nullable=False, comment="左上X（原图像素）")
    y1: Mapped[float] = mapped_column(Float, nullable=False, comment="左上Y（原图像素）")
    x2: Mapped[float] = mapped_column(Float, nullable=False, comment="右下X（原图像素）")
    y2: Mapped[float] = mapped_column(Float, nullable=False, comment="右下Y（原图像素）")
    width_mm: Mapped[float | None] = mapped_column(Float, nullable=True, comment="宽（毫米）")
    height_mm: Mapped[float | None] = mapped_column(Float, nullable=True, comment="高（毫米）")
    area_mm2: Mapped[float | None] = mapped_column(Float, nullable=True, comment="面积（平方毫米）")
    is_false_positive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="人工标记误报"
    )
    is_missed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="人工补录漏报"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="排序"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
